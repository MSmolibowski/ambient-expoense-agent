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
import re
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

# Default location to global
os.environ["GOOGLE_CLOUD_LOCATION"] = "global"


# 1. Input Parsing Node
@node
def parse_event(ctx: Context, node_input: types.Content) -> dict:
    """Parses incoming JSON events that may be base64-encoded (from Pub/Sub) or plain JSON."""
    text = ""
    if node_input.parts:
        text = node_input.parts[0].text or ""
        
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        payload = {"data": {"description": text}}
        
    data = payload.get("data", payload)
    
    if isinstance(data, str):
        try:
            decoded_bytes = base64.b64decode(data.encode("utf-8"))
            data = json.loads(decoded_bytes.decode("utf-8"))
        except Exception:
            pass
            
    if not isinstance(data, dict):
        data = {"description": str(data)}
        
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
    state_delta = {"expense": node_input}
    
    if amount < THRESHOLD:
        return Event(output=node_input, route="auto_approve", state=state_delta)
    else:
        return Event(output=node_input, route="llm_review", state=state_delta)


# Regex rules for personal data scrubbing
SSN_REGEX = re.compile(r'\b\d{3}-\d{2}-\d{4}\b|\b\d{9}\b')
CC_REGEX = re.compile(r'\b(?:\d[ -]*?){13,19}\b')


# 3. Security Checkpoint Node (Scrub PII and detect prompt injection)
@node
def security_checkpoint(ctx: Context, node_input: dict) -> Event:
    """Security checkpoint that scrubs PII and defends against prompt injection."""
    description = node_input.get("description", "")
    redacted_categories = []
    scrubbed_description = description
    
    # Scrub SSNs
    if SSN_REGEX.search(scrubbed_description):
        scrubbed_description = SSN_REGEX.sub("[REDACTED SSN]", scrubbed_description)
        redacted_categories.append("SSN")
        
    # Scrub Credit Cards
    if CC_REGEX.search(scrubbed_description):
        scrubbed_description = CC_REGEX.sub("[REDACTED CREDIT CARD]", scrubbed_description)
        redacted_categories.append("CREDIT_CARD")
        
    # Update description in node output & state
    updated_expense = node_input.copy()
    updated_expense["description"] = scrubbed_description
    
    state_delta = {
        "expense": updated_expense,
        "redacted_categories": redacted_categories
    }
    
    # Prompt injection detection triggers
    injection_triggers = [
        "ignore", "bypass", "override", "system prompt", "instead of",
        "instruction", "auto-approve", "auto approve", "force approval",
        "you are now", "new rule", "disable rules"
    ]
    is_injection = any(trigger in description.lower() for trigger in injection_triggers)
    
    if is_injection:
        # Raise alert, bypass LLM, route straight to human manager review
        state_delta["security_alert"] = True
        state_delta["risk_assessment"] = {
            "risk_score": 10,
            "risk_factors": ["PROMPT INJECTION ATTEMPT DETECTED"],
            "alert_triggered": True,
            "reason": "The expense description contained keywords matching prompt injection attempts."
        }
        return Event(output=updated_expense, route="security_event", state=state_delta)
    else:
        return Event(output=updated_expense, route="clean", state=state_delta)


# 4. LLM Node for Risk Judgment
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


# 5. Human-in-the-Loop manager approval node
@node(rerun_on_resume=True)
async def manager_approval(ctx: Context, node_input: dict):
    """Pauses the workflow using RequestInput to get approval from a manager."""
    expense = ctx.state.get("expense", {})
    risk_assessment = ctx.state.get("risk_assessment", {})
    
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
        )
        # Append security alert details if present
        if ctx.state.get("security_alert"):
            message += f"- [SECURITY ALERT]: Prompt injection pattern detected!\n"
            
        message += f"\nDo you approve this expense? (yes/no)"
        yield RequestInput(interrupt_id="approved", message=message)
        return

    approved_val = ctx.resume_inputs["approved"]
    if isinstance(approved_val, dict):
        manager_response = approved_val.get("approved") or approved_val.get("value")
        if manager_response is None and approved_val:
            manager_response = next(iter(approved_val.values()))
    else:
        manager_response = approved_val

    if manager_response:
        manager_response = str(manager_response).strip().lower()
    else:
        manager_response = ""
    
    if manager_response in ["yes", "y", "approve"]:
        yield Event(output={"status": "approved", "expense": expense})
    else:
        yield Event(output={"status": "rejected", "expense": expense})


# 6. Final Outcome Recording Node
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
        Edge(from_node=evaluate_expense, to_node=security_checkpoint, route="llm_review"),
        Edge(from_node=evaluate_expense, to_node=record_outcome, route="auto_approve"),
        Edge(from_node=security_checkpoint, to_node=manager_approval, route="security_event"),
        Edge(from_node=security_checkpoint, to_node=llm_review_node, route="clean"),
        Edge(from_node=llm_review_node, to_node=manager_approval),
        Edge(from_node=manager_approval, to_node=record_outcome),
    ]
)

# App wrapping the workflow agent
app = App(
    root_agent=root_agent,
    name="expense_agent",
    resumability_config=ResumabilityConfig(is_resumable=True)  # Required for human-in-the-loop pauses
)
