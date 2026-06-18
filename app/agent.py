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
from pydantic import BaseModel, Field
from google.adk.workflow import Workflow, START, node
from google.adk.agents import LlmAgent
from google.adk.events.event import Event
from google.adk.events.request_input import RequestInput
from google.adk.agents.context import Context
from google.adk.apps import App, ResumabilityConfig
from google.genai import types
import google.auth
from dotenv import load_dotenv

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

if project_id:
    os.environ["GOOGLE_CLOUD_PROJECT"] = project_id

# If GEMINI_API_KEY is present, we use AI Studio by setting GOOGLE_GENAI_USE_VERTEXAI = "False"
if "GEMINI_API_KEY" in os.environ and not os.environ.get("GOOGLE_GENAI_USE_VERTEXAI"):
    os.environ["GOOGLE_GENAI_USE_VERTEXAI"] = "False"
else:
    os.environ["GOOGLE_GENAI_USE_VERTEXAI"] = os.environ.get("GOOGLE_GENAI_USE_VERTEXAI", "True")

# Default location to global if not set
os.environ["GOOGLE_CLOUD_LOCATION"] = os.environ.get("GOOGLE_CLOUD_LOCATION", "global")


# Schema for structured output from LLM Agent node
class ExpenseDetails(BaseModel):
    item: str = Field(description="The item or service purchased")
    amount: float = Field(description="The cost/amount of the expense")
    category: str = Field(description="The category of the expense (e.g., meals, travel, software)")


# LLM Agent Node to parse input
parse_expense = LlmAgent(
    name="parse_expense",
    model="gemini-flash-latest",
    instruction="Extract expense details from the user's description. Identify the item, amount, and category.",
    output_schema=ExpenseDetails,
    output_key="expense_details",
)


# Function Node: Evaluate Expense and Route
@node
def evaluate_expense(ctx: Context, node_input: dict) -> Event:
    """Evaluates the parsed expense details.
    
    If the amount is greater than $100, we route to manager approval.
    Otherwise, we process it immediately.
    """
    amount = node_input.get("amount", 0.0)
    
    # Save the parsed details in state
    state_delta = {"expense_details": node_input}
    
    if amount > 100.0:
        # Route to manager approval
        return Event(output=node_input, route="needs_approval", state=state_delta)
    else:
        # Route directly to process payment
        return Event(output=node_input, route="auto_approve", state=state_delta)


# Function Node with rerun_on_resume=True for Human-in-the-Loop step
@node(rerun_on_resume=True)
async def manager_approval(ctx: Context, node_input: dict):
    """Asks for manager approval for expensive items using RequestInput."""
    # Check if the user/manager has responded yet
    if not ctx.resume_inputs or "approved" not in ctx.resume_inputs:
        # Ask the manager for input
        amount = node_input.get("amount", 0.0)
        item = node_input.get("item", "unknown item")
        message = f"Expense of ${amount:.2f} for '{item}' requires manager approval. Do you approve? (yes/no)"
        yield RequestInput(interrupt_id="approved", message=message)
        return

    # Once resumed, retrieve the manager's response
    manager_response = ctx.resume_inputs["approved"].strip().lower()
    
    if manager_response in ["yes", "y", "approve"]:
        yield Event(output={"status": "approved", "details": node_input})
    else:
        yield Event(output={"status": "rejected", "details": node_input})


# Function Node: Process / Record Payment
@node
def record_expense(ctx: Context, node_input: dict):
    """Final node that records the outcome of the expense report."""
    status = node_input.get("status")
    if status is None:
        status = "auto_approved"
        details = node_input
    else:
        details = node_input.get("details", {})
    
    amount = details.get("amount", 0.0)
    item = details.get("item", "unknown item")
    
    if status == "rejected":
        result = f"Expense of ${amount:.2f} for '{item}' was rejected."
    elif status == "approved":
        result = f"Expense of ${amount:.2f} for '{item}' was approved by manager and recorded."
    else:
        result = f"Expense of ${amount:.2f} for '{item}' was auto-approved and recorded."

    yield Event(content=types.Content(role='model', parts=[types.Part.from_text(text=result)]))
    yield Event(output=result)


# Define the workflow graph
root_agent = Workflow(
    name="root_agent",
    edges=[
        (START, parse_expense),
        (parse_expense, evaluate_expense),
        (evaluate_expense, manager_approval, "needs_approval"),
        (evaluate_expense, record_expense, "auto_approve"),
        (manager_approval, record_expense),
    ]
)

# App wrapping the workflow agent
app = App(
    root_agent=root_agent,
    name="app",
    resumability_config=ResumabilityConfig(is_resumable=True)  # Required for human-in-the-loop
)
