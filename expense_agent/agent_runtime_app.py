# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
import logging
import os
import json
import base64
import uuid
from typing import Any, Optional

import vertexai
from dotenv import load_dotenv

# Load environment variables first
load_dotenv()

# Setup Vertex AI with a default project to bypass Application Default Credentials checks during import
project_id = os.environ.get("GOOGLE_CLOUD_PROJECT")
if not project_id or project_id.startswith("AQ."):
    project_id = "ambient-expense-agent"
vertexai.init(project=project_id)

from google.adk.runners import Runner
from google.adk.cli.fast_api import get_fast_api_app
from google.adk.cli.utils.service_factory import (
    create_session_service_from_options,
    create_artifact_service_from_options,
)
from google.adk.artifacts import GcsArtifactService, InMemoryArtifactService
from vertexai.agent_engines.templates.adk import AdkApp
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field
from google.genai import types
from google.adk.utils.context_utils import Aclosing

from expense_agent.agent import app as adk_app
from expense_agent.app_utils.telemetry import setup_telemetry
from expense_agent.app_utils.typing import Feedback

# Setup standard logging for console logs
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    force=True
)
logger = logging.getLogger(__name__)


class AgentEngineApp(AdkApp):
    def set_up(self) -> None:
        """Initialize the agent engine app with logging and telemetry."""
        vertexai.init()
        # Telemetry: Set otel_to_cloud=False by disabling telemetry in ADK setup
        os.environ["GOOGLE_CLOUD_AGENT_ENGINE_ENABLE_TELEMETRY"] = "false"
        setup_telemetry()
        super().set_up()
        self.logger = logger
        if gemini_location:
            os.environ["GOOGLE_CLOUD_LOCATION"] = gemini_location

    def register_feedback(self, feedback: dict[str, Any]) -> None:
        """Collect and log feedback."""
        feedback_obj = Feedback.model_validate(feedback)
        if os.environ.get("INTEGRATION_TEST") == "TRUE":
            logging.info(f"[TEST MOCK LOG] feedback: {feedback_obj.model_dump()}")
            return
        try:
            self.logger.info(f"Feedback registered: {feedback_obj.model_dump()}")
        except Exception as e:
            logging.warning(f"Failed to log feedback: {e}")

    def register_operations(self) -> dict[str, list[str]]:
        """Registers the operations of the Agent."""
        operations = super().register_operations()
        operations[""] = [*operations.get("", []), "register_feedback"]
        return operations

    def clone(self) -> "AgentEngineApp":
        """Returns a clone of the Agent Runtime application."""
        return self


gemini_location = "global"
logs_bucket_name = os.environ.get("LOGS_BUCKET_NAME")
agent_runtime = AgentEngineApp(
    app=adk_app,
    artifact_service_builder=lambda: (
        GcsArtifactService(bucket_name=logs_bucket_name)
        if logs_bucket_name
        else InMemoryArtifactService()
    ),
)

# ----------------- FULL DEV UI & LOCAL WEB SERVICE -----------------
# Create the standard ADK FastAPI app with the Dev UI enabled
app = get_fast_api_app(
    agents_dir="expense_agent",
    web=True,
)

# Initialize local services pointing to the same SQLite DB file so that
# the sessions created by the Pub/Sub trigger are visible in the Dev UI.
session_service = create_session_service_from_options(
    base_dir="expense_agent",
    use_local_storage=True,
)
artifact_service = create_artifact_service_from_options(
    base_dir="expense_agent",
    use_local_storage=True,
)
runner = Runner(
    app=adk_app,
    session_service=session_service,
    artifact_service=artifact_service
)


class PubSubMessage(BaseModel):
    data: Optional[str] = Field(default=None, description="Base64-encoded message data.")
    attributes: Optional[dict[str, str]] = Field(default=None, description="Message attributes.")
    messageId: Optional[str] = Field(default=None, description="Pub/Sub message ID.")
    publishTime: Optional[str] = Field(default=None, description="Publish timestamp.")


class PubSubTriggerRequest(BaseModel):
    message: PubSubMessage
    subscription: Optional[str] = Field(
        default=None,
        description="Full subscription name (e.g. projects/p/subscriptions/s)."
    )


def normalize_subscription_name(sub_path: Optional[str]) -> str:
    """Normalize fully-qualified subscription path down to a short name."""
    if not sub_path:
        return "pubsub-caller"
    return sub_path.split("/")[-1]


@app.post("/apps/{app_name}/trigger/pubsub")
@app.post("/trigger/pubsub")
async def trigger_pubsub(req: PubSubTriggerRequest, app_name: Optional[str] = None):
    subscription = req.subscription or "pubsub-caller"
    user_id = normalize_subscription_name(subscription)

    decoded_data = None
    if req.message.data:
        try:
            decoded_bytes = base64.b64decode(req.message.data)
            decoded_data = decoded_bytes.decode("utf-8")
        except Exception as e:
            logger.error(f"Failed to decode base64 message data: {e}")
            raise HTTPException(status_code=400, detail=f"Invalid base64 message data: {e}")

    try:
        data_payload = json.loads(decoded_data) if decoded_data else {}
    except json.JSONDecodeError:
        data_payload = decoded_data

    message_text = json.dumps({
        "data": data_payload,
        "attributes": req.message.attributes or {}
    })

    # Unique session ID for tracking
    session_id = f"pubsub-session-{uuid.uuid4()}"
    logger.info(f"Received Pub/Sub trigger for subscription: {subscription} (normalized to short name: {user_id})")
    logger.info(f"Starting new workflow session: {session_id}")

    # Create session using the shared session service
    await session_service.create_session(
        app_name=adk_app.name,
        user_id=user_id,
        session_id=session_id
    )

    new_message = types.Content(
        role="user",
        parts=[types.Part.from_text(text=message_text)]
    )

    events = []
    try:
        async with Aclosing(
            runner.run_async(
                user_id=user_id,
                session_id=session_id,
                new_message=new_message
            )
        ) as agen:
            async for event in agen:
                events.append(event)
                # Print node execution status to console
                if event.node_info:
                    logger.info(f"Node execution: {event.node_info.path}")
                if event.content and event.content.parts:
                    for part in event.content.parts:
                        if part.text:
                            logger.info(f"Output Event Content: {part.text.strip()}")
    except Exception as e:
        logger.error(f"Agent processing failed: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Agent processing failed: {e}")

    logger.info(f"Workflow finished successfully for session: {session_id}")
    return {"status": "success", "session_id": session_id}
