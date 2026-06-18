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

import os
import json
import base64
from pydantic import BaseModel, Field
from google.adk.workflow import Workflow, START, node, Edge
from google.adk.agents import LlmAgent
from google.adk.events.event import Event
from google.adk.events.request_input import RequestInput
from google.adk.agents.context import Context
from google.adk.apps import App, ResumabilityConfig
from google.genai import types
import google.auth
from dotenv import load_dotenv

from expense_agent.config import THRESHOLD, MODEL

# Load environment variables from .env file
load_dotenv()

# Setup local authentication and project ID
project_id = os.environ.get("GOOGLE_CLOUD_PROJECT")
if not project_id:
    try:
        _, credentials_project_id = google.auth.default()
        project_id = credentials_project_id
    except Exception:
        pass
if not project_id:
    project_id = "ambient-expense-agent"

os.environ["GOOGLE_CLOUD_PROJECT"] = project_id

# If GEMINI_API_KEY is present, we use AI Studio by setting GOOGLE_GENAI_USE_VERTEXAI = "False"
if "GEMINI_API_KEY" in os.environ and not os.environ.get("GOOGLE_GENAI_USE_VERTEXAI"):
    os.environ["GOOGLE_GENAI_USE_VERTEXAI"] = "False"
else:
    os.environ["GOOGLE_GENAI_USE_VERTEXAI"] = os.environ.get("GOOGLE_GENAI_USE_VERTEXAI", "True")

# Default location to global if not set
os.environ["GOOGLE_CLOUD_LOCATION"] = os.environ.get("GOOGLE_CLOUD_LOCATION", "global")


# 1. Input Parsing Node
@node
def parse_event(ctx: Context, node_input: types.Content) -> dict:
    """Parses incoming JSON events that may be base64-encoded (from Pub/Sub) or plain JSON."""
    # Retrieve the text prompt or payload from the message content
    text = ""
    if node_input.parts:
        text = node_input.parts[0].text or ""
        
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        # If not valid JSON, treat the text as description
        payload = {"data": {"description": text}}
        
    # Details sit under a "data" key
    data = payload.get("data", payload)
    
    # Handle base64 encoded data (standard in real Google Cloud Pub/Sub)
    if isinstance(data, str):
        try:
            decoded_bytes = base64.b64decode(data.encode("utf-8"))
            data = json.loads(decoded_bytes.decode("utf-8"))
        except Exception:
            pass
            
    if not isinstance(data, dict):
        data = {"description": str(data)}
        
    # Extract fields with safe fallbacks
    expense = {
        "amount": float(data.get("amount", 0.0)),
        "submitter": str(data.get("submitter", "Unknown")),
        "category": str(data.get("category", "General")),
        "description": str(data.get("description", "No description")),
        "date": str(data.get("date", "Unknown")),
    }
    return expense


# 2. Rule Evaluation and Routing Node
@node
def evaluate_expense(ctx: Context, node_input: dict) -> Event:
    """Applies routing logic based on the expense threshold."""
    amount = node_input.get("amount", 0.0)
    
    # Keep the parsed expense in session state for later nodes
    state_delta = {"expense": node_input}
    
    # Apply threshold rule (routing is handled in code)
    if amount < THRESHOLD:
        return Event(output=node_input, route="auto_approve", state=state_delta)
    else:
        return Event(output=node_input, route="llm_review", state=state_delta)


# 3. LLM Node for Risk Judgment
class RiskAssessment(BaseModel):
    risk_score: int = Field(description="Risk score from 1 (lowest) to 10 (highest)")
    risk_factors: list[str] = Field(description="List of risk factors or anomalies identified in the expense")
    alert_triggered: bool = Field(description="True if an alert is raised due to high risk or policy violation")
    reason: str = Field(description="Detailed explanation of the risk judgment")


llm_review_node = LlmAgent(
    name="llm_review_node",
    model=MODEL,
    instruction="""
    Review the provided expense details (amount, submitter, category, description, and date) for risk factors,
    anomalies, or policy violations. Provide a structured risk assessment and trigger an alert if appropriate.
    """,
    output_schema=RiskAssessment,
    output_key="risk_assessment",
)


# 4. Human-in-the-Loop manager approval node
@node(rerun_on_resume=True)
async def manager_approval(ctx: Context, node_input: dict):
    """Pauses the workflow using RequestInput to get approval from a manager."""
    expense = ctx.state.get("expense", {})
    risk_assessment = ctx.state.get("risk_assessment", {})
    
    # Pause the workflow if we don't have approval input yet
    if not ctx.resume_inputs or "approved" not in ctx.resume_inputs:
        amount = expense.get("amount", 0.0)
        item = expense.get("description", "unknown")
        risk_score = risk_assessment.get("risk_score", 0)
        alert = "ALERT TRIGGERED" if risk_assessment.get("alert_triggered") else "No alert"
        
        message = (
            f"Manager Approval Required:\n"
            f"- Submitter: {expense.get('submitter')}\n"
            f"- Expense: {item} (${amount:.2f})\n"
            f"- Category: {expense.get('category')}\n"
            f"- Risk Score: {risk_score}/10 ({alert})\n"
            f"- Reason: {risk_assessment.get('reason')}\n"
            f"\nDo you approve this expense? (yes/no)"
        )
        yield RequestInput(interrupt_id="approved", message=message)
        return

    # Once resumed, record response
    manager_response = ctx.resume_inputs["approved"].strip().lower()
    
    if manager_response in ["yes", "y", "approve"]:
        yield Event(output={"status": "approved", "expense": expense})
    else:
        yield Event(output={"status": "rejected", "expense": expense})


# 5. Final Outcome Recording Node
@node
def record_outcome(ctx: Context, node_input: dict):
    """Final node that records the outcome of the expense report."""
    status = node_input.get("status")
    if status is None:
        status = "auto_approved"
        expense = node_input
    else:
        expense = node_input.get("expense", {})
    
    amount = expense.get("amount", 0.0)
    description = expense.get("description", "unknown")
    submitter = expense.get("submitter", "unknown")
    
    if status == "approved":
        result = f"Expense of ${amount:.2f} for '{description}' submitted by {submitter} was APPROVED."
    elif status == "rejected":
        result = f"Expense of ${amount:.2f} for '{description}' submitted by {submitter} was REJECTED."
    else:
        result = f"Expense of ${amount:.2f} for '{description}' submitted by {submitter} was AUTO-APPROVED."

    # Yield content event for Web UI and output event for runtime downstream
    yield Event(content=types.Content(role='model', parts=[types.Part.from_text(text=result)]))
    yield Event(output=result)


# Define the Workflow Graph
root_agent = Workflow(
    name="root_agent",
    edges=[
        Edge(from_node=START, to_node=parse_event),
        Edge(from_node=parse_event, to_node=evaluate_expense),
        Edge(from_node=evaluate_expense, to_node=llm_review_node, route="llm_review"),
        Edge(from_node=evaluate_expense, to_node=record_outcome, route="auto_approve"),
        Edge(from_node=llm_review_node, to_node=manager_approval),
        Edge(from_node=manager_approval, to_node=record_outcome),
    ]
)

# App wrapping the workflow agent
app = App(
    root_agent=root_agent,
    name="app",
    resumability_config=ResumabilityConfig(is_resumable=True)  # Required for human-in-the-loop pauses
)
