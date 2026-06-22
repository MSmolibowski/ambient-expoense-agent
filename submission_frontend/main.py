import os
import re
import sys
import logging
from typing import Any, Optional
from pydantic import BaseModel

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse

import vertexai
from vertexai.preview import reasoning_engines
from google.cloud.aiplatform_v1beta1 import types as aip_types
from google.adk.sessions.vertex_ai_session_service import VertexAiSessionService
from vertexai.reasoning_engines import _utils

# Setup basic logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI(title="Ambient Expense Manager Dashboard")

# Read configuration from environment variables
PROJECT_ID = os.environ.get("GOOGLE_CLOUD_PROJECT", "gen-lang-client-0193227087")
AGENT_RUNTIME_ID = os.environ.get("AGENT_RUNTIME_ID")

if not AGENT_RUNTIME_ID:
    # Fallback to local metadata file if present
    metadata_path = "C:/Users/Hp/source/repos/ambient-expense-agent/deployment_metadata.json"
    if os.path.exists(metadata_path):
        import json
        try:
            with open(metadata_path, "r") as f:
                metadata = json.load(f)
                AGENT_RUNTIME_ID = metadata.get("remote_agent_runtime_id")
        except Exception as e:
            logger.error(f"Failed to read local deployment metadata: {e}")

if not AGENT_RUNTIME_ID:
    logger.error("AGENT_RUNTIME_ID environment variable is not set and no valid metadata file found.")
    sys.exit("Error: AGENT_RUNTIME_ID must be configured.")

# Extract location/region and short engine ID from the full resource path if possible
REGION = "us-east1"
ENGINE_SHORT_ID = AGENT_RUNTIME_ID
match_loc = re.search(r"locations/([a-zA-Z0-9-_]+)/reasoningEngines", AGENT_RUNTIME_ID)
if match_loc:
    REGION = match_loc.group(1)

match_id = re.search(r"reasoningEngines/(\d+)", AGENT_RUNTIME_ID)
if match_id:
    ENGINE_SHORT_ID = match_id.group(1)

logger.info(f"Initializing Vertex AI with Project={PROJECT_ID}, Location={REGION}, Engine ID={ENGINE_SHORT_ID}")
vertexai.init(project=PROJECT_ID, location=REGION)

# Setup Session Service
session_service = VertexAiSessionService(
    project=PROJECT_ID,
    location=REGION,
    agent_engine_id=ENGINE_SHORT_ID
)



class ActionPayload(BaseModel):
    approved: bool
    interrupt_id: str


def stream_query_remote(engine, method_name, **kwargs):
    response = engine.execution_api_client.stream_query_reasoning_engine(
        request=aip_types.StreamQueryReasoningEngineRequest(
            name=engine.resource_name,
            input=kwargs,
            class_method=method_name,
        ),
    )
    for chunk in response:
        for parsed_json in _utils.yield_parsed_json(chunk):
            if parsed_json is not None:
                yield parsed_json


# ----------------- UI / HTML Endpoint -----------------
@app.get("/", response_class=HTMLResponse)
async def get_dashboard():
    html_content = f"""
    <!DOCTYPE html>
    <html lang="en">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>Manager Dashboard - Ambient Expense Agent</title>
        <link href="https://fonts.googleapis.com/css2?family=Outfit:wght@300;400;600;800&display=swap" rel="stylesheet">
        <style>
            :root {{
                --bg-color: #0A0B10;
                --card-bg: rgba(255, 255, 255, 0.03);
                --card-border: rgba(255, 255, 255, 0.08);
                --text-color: #F3F4F6;
                --text-muted: #9CA3AF;
                --primary: #6366F1;
                --primary-hover: #4F46E5;
                --success: #10B981;
                --danger: #EF4444;
            }}

            * {{
                box-sizing: border-box;
                margin: 0;
                padding: 0;
            }}

            body {{
                background-color: var(--bg-color);
                color: var(--text-color);
                font-family: 'Outfit', sans-serif;
                overflow-x: hidden;
                min-height: 100vh;
                background-image: 
                    radial-gradient(circle at 10% 20%, rgba(99, 102, 241, 0.1) 0%, transparent 40%),
                    radial-gradient(circle at 90% 80%, rgba(16, 185, 129, 0.08) 0%, transparent 45%);
            }}

            .container {{
                max-width: 1200px;
                margin: 0 auto;
                padding: 40px 20px;
            }}

            header {{
                display: flex;
                justify-content: space-between;
                align-items: center;
                margin-bottom: 40px;
                border-bottom: 1px solid var(--card-border);
                padding-bottom: 20px;
            }}

            h1 {{
                font-weight: 800;
                font-size: 2.5rem;
                background: linear-gradient(135deg, #FFF 60%, var(--primary));
                -webkit-background-clip: text;
                -webkit-text-fill-color: transparent;
                letter-spacing: -0.05em;
            }}

            .subtitle {{
                font-size: 0.85rem;
                color: var(--text-muted);
                margin-top: 5px;
                font-family: monospace;
                word-break: break-all;
            }}

            .btn-refresh {{
                background: var(--card-bg);
                border: 1px solid var(--card-border);
                color: var(--text-color);
                padding: 10px 20px;
                border-radius: 9999px;
                cursor: pointer;
                font-weight: 600;
                display: flex;
                align-items: center;
                gap: 8px;
                transition: all 0.2s ease;
            }}

            .btn-refresh:hover {{
                background: rgba(255, 255, 255, 0.08);
                border-color: var(--primary);
                transform: translateY(-1px);
            }}

            .dashboard-grid {{
                display: grid;
                grid-template-columns: repeat(auto-fill, minmax(360px, 1fr));
                gap: 24px;
            }}

            .card {{
                background: var(--card-bg);
                border: 1px solid var(--card-border);
                backdrop-filter: blur(20px);
                -webkit-backdrop-filter: blur(20px);
                border-radius: 20px;
                padding: 24px;
                display: flex;
                flex-direction: column;
                transition: all 0.3s cubic-bezier(0.4, 0, 0.2, 1);
                position: relative;
                overflow: hidden;
            }}

            .card:hover {{
                transform: translateY(-4px);
                border-color: rgba(99, 102, 241, 0.3);
                box-shadow: 0 10px 30px rgba(0, 0, 0, 0.5), 0 0 20px rgba(99, 102, 241, 0.15);
            }}

            .card::before {{
                content: '';
                position: absolute;
                top: 0;
                left: 0;
                width: 100%;
                height: 4px;
                background: linear-gradient(90deg, var(--primary), var(--success));
                opacity: 0.7;
            }}

            .card-header {{
                display: flex;
                justify-content: space-between;
                align-items: flex-start;
                margin-bottom: 20px;
            }}

            .category-tag {{
                background: rgba(99, 102, 241, 0.15);
                color: #A5B4FC;
                padding: 4px 12px;
                border-radius: 9999px;
                font-size: 0.8rem;
                font-weight: 600;
                text-transform: uppercase;
            }}

            .amount {{
                font-size: 1.8rem;
                font-weight: 800;
                color: #FFF;
            }}

            .detail-row {{
                display: flex;
                justify-content: space-between;
                margin-bottom: 12px;
                font-size: 0.95rem;
            }}

            .detail-label {{
                color: var(--text-muted);
            }}

            .detail-value {{
                font-weight: 600;
                color: var(--text-color);
            }}

            .description-box {{
                background: rgba(0, 0, 0, 0.2);
                padding: 12px;
                border-radius: 10px;
                margin-top: 10px;
                margin-bottom: 20px;
                font-size: 0.9rem;
                border: 1px solid rgba(255, 255, 255, 0.03);
                color: #D1D5DB;
            }}

            .risk-badge {{
                display: flex;
                align-items: center;
                gap: 6px;
                background: rgba(239, 68, 68, 0.1);
                border: 1px solid rgba(239, 68, 68, 0.2);
                color: #FCA5A5;
                padding: 4px 10px;
                border-radius: 8px;
                font-size: 0.85rem;
                font-weight: 600;
            }}

            .risk-badge.low {{
                background: rgba(16, 185, 129, 0.1);
                border-color: rgba(16, 185, 129, 0.2);
                color: #A7F3D0;
            }}

            .actions-group {{
                display: flex;
                gap: 12px;
                margin-top: auto;
            }}

            .btn {{
                flex: 1;
                padding: 12px;
                border-radius: 12px;
                border: none;
                font-weight: 600;
                cursor: pointer;
                transition: all 0.2s ease;
                display: flex;
                justify-content: center;
                align-items: center;
                gap: 8px;
                color: #FFF;
            }}

            .btn-approve {{
                background: linear-gradient(135deg, #10B981, #059669);
                box-shadow: 0 4px 12px rgba(16, 185, 129, 0.2);
            }}

            .btn-approve:hover {{
                transform: translateY(-1px);
                box-shadow: 0 6px 16px rgba(16, 185, 129, 0.35);
            }}

            .btn-reject {{
                background: linear-gradient(135deg, #EF4444, #DC2626);
                box-shadow: 0 4px 12px rgba(239, 68, 68, 0.2);
            }}

            .btn-reject:hover {{
                transform: translateY(-1px);
                box-shadow: 0 6px 16px rgba(239, 68, 68, 0.35);
            }}

            .no-data {{
                text-align: center;
                grid-column: 1 / -1;
                padding: 80px 0;
                color: var(--text-muted);
            }}

            /* Slide-out Drawer */
            .drawer-overlay {{
                position: fixed;
                top: 0;
                left: 0;
                width: 100vw;
                height: 100vh;
                background: rgba(0, 0, 0, 0.7);
                backdrop-filter: blur(5px);
                z-index: 100;
                opacity: 0;
                pointer-events: none;
                transition: opacity 0.3s ease;
            }}

            .drawer-overlay.active {{
                opacity: 1;
                pointer-events: auto;
            }}

            .drawer {{
                position: fixed;
                top: 0;
                right: -500px;
                width: 500px;
                height: 100vh;
                background: #11131E;
                border-left: 1px solid var(--card-border);
                box-shadow: -10px 0 30px rgba(0, 0, 0, 0.5);
                z-index: 101;
                transition: transform 0.3s cubic-bezier(0.4, 0, 0.2, 1);
                padding: 40px 30px;
                display: flex;
                flex-direction: column;
            }}

            .drawer.active {{
                transform: translateX(-500px);
            }}

            .drawer-header {{
                display: flex;
                justify-content: space-between;
                align-items: center;
                margin-bottom: 30px;
            }}

            .drawer-title {{
                font-size: 1.5rem;
                font-weight: 800;
                color: #FFF;
            }}

            .btn-close {{
                background: none;
                border: none;
                color: var(--text-muted);
                font-size: 1.5rem;
                cursor: pointer;
            }}

            .drawer-content {{
                flex-grow: 1;
                overflow-y: auto;
            }}

            .result-block {{
                background: rgba(255, 255, 255, 0.02);
                border: 1px solid var(--card-border);
                border-radius: 12px;
                padding: 20px;
                font-family: monospace;
                white-space: pre-wrap;
                color: #E5E7EB;
                font-size: 0.9rem;
                line-height: 1.5;
                margin-top: 15px;
            }}

            .spinner {{
                border: 3px solid rgba(255, 255, 255, 0.3);
                border-radius: 50%;
                border-top: 3px solid #fff;
                width: 18px;
                height: 18px;
                animation: spin 1s linear infinite;
                display: none;
            }}

            @keyframes spin {{
                0% {{ transform: rotate(0deg); }}
                100% {{ transform: rotate(360deg); }}
            }}
        </style>
    </head>
    <body>
        <div class="container">
            <header>
                <div>
                    <h1>Expense Approval Dashboard</h1>
                    <div class="subtitle">Agent ID: {AGENT_RUNTIME_ID}</div>
                </div>
                <button class="btn-refresh" onclick="fetchPending()">
                    <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21.5 2v6h-6M21.34 15.57a10 10 0 1 1-.57-8.38l5.67-5.67"/></svg>
                    Refresh
                </button>
            </header>

            <div class="dashboard-grid" id="dashboard-grid">
                <div class="no-data">Loading pending approvals...</div>
            </div>
        </div>

        <!-- Compliance drawer overlay -->
        <div class="drawer-overlay" id="drawer-overlay" onclick="closeDrawer()"></div>
        
        <!-- Compliance drawer -->
        <div class="drawer" id="drawer">
            <div class="drawer-header">
                <div class="drawer-title">Compliance Review</div>
                <button class="btn-close" onclick="closeDrawer()">&times;</button>
            </div>
            <div class="drawer-content" id="drawer-content-box">
                <!-- Content will be rendered dynamically -->
            </div>
        </div>

        <script>
            async function fetchPending() {{
                const grid = document.getElementById('dashboard-grid');
                grid.innerHTML = '<div class="no-data">Fetching pending approvals...</div>';
                try {{
                    const response = await fetch('/api/pending');
                    const data = await response.json();
                    if (data.length === 0) {{
                        grid.innerHTML = '<div class="no-data">No pending approvals found.</div>';
                        return;
                    }}
                    grid.innerHTML = '';
                    data.forEach(item => {{
                        const card = document.createElement('div');
                        card.className = 'card';
                        
                        const expense = item.expense || {{}};
                        const risk = item.risk_assessment || {{}};
                        const riskScore = risk.risk_score || 1;
                        const isHighRisk = riskScore >= 5 || risk.alert_triggered;
                        
                        card.innerHTML = `
                            <div class="card-header">
                                <span class="category-tag">${{expense.category || 'General'}}</span>
                                <div class="risk-badge ${{isHighRisk ? '' : 'low'}}">
                                    <span>Risk: ${{riskScore}}/10</span>
                                </div>
                            </div>
                            <div class="amount">$${{(expense.amount || 0).toFixed(2)}}</div>
                            <div style="margin-top: 15px;">
                                <div class="detail-row">
                                    <span class="detail-label">Submitter</span>
                                    <span class="detail-value">${{expense.submitter || 'Unknown'}}</span>
                                </div>
                                <div class="detail-row">
                                    <span class="detail-label">Date</span>
                                    <span class="detail-value">${{expense.date || 'Unknown'}}</span>
                                </div>
                            </div>
                            <div class="description-box">
                                <strong>Description:</strong> ${{expense.description || 'No description'}}
                            </div>
                            ${{risk.reason ? `
                            <div style="margin-bottom: 20px; font-size: 0.85rem; color: var(--text-muted); border-left: 2px solid var(--primary); padding-left: 10px;">
                                <strong>Assessment:</strong> ${{risk.reason}}
                            </div>
                            ` : ''}}
                            <div class="actions-group">
                                <button class="btn btn-reject" onclick="takeAction('${{item.session_id}}', '${{item.interrupt_id}}', false, this)">
                                    <div class="spinner"></div>
                                    <span>Reject</span>
                                </button>
                                <button class="btn btn-approve" onclick="takeAction('${{item.session_id}}', '${{item.interrupt_id}}', true, this)">
                                    <div class="spinner"></div>
                                    <span>Approve</span>
                                </button>
                            </div>
                        `;
                        grid.appendChild(card);
                    }});
                }} catch (error) {{
                    grid.innerHTML = '<div class="no-data" style="color: var(--danger)">Error fetching pending approvals.</div>';
                    console.error(error);
                }}
            }}

            async function takeAction(sessionId, interruptId, approved, button) {{
                const spinner = button.querySelector('.spinner');
                const btnText = button.querySelector('span');
                spinner.style.display = 'block';
                btnText.style.display = 'none';
                button.disabled = true;
                
                // Disable sibling button
                const card = button.closest('.card');
                const buttons = card.querySelectorAll('.btn');
                buttons.forEach(btn => btn.disabled = true);

                try {{
                    const response = await fetch(`/api/action/${{sessionId}}`, {{
                        method: 'POST',
                        headers: {{
                            'Content-Type': 'application/json'
                        }},
                        body: JSON.stringify({{
                            approved: approved,
                            interrupt_id: interruptId
                        }})
                    }});
                    
                    const data = await response.json();
                    
                    if (response.ok && data.status === 'success') {{
                        // Open compliance drawer
                        showDrawer(approved, data.outcome_text || JSON.stringify(data.raw_chunks, null, 2));
                        // Refresh dashboard
                        fetchPending();
                    }} else {{
                        alert('Action failed: ' + (data.detail || 'Unknown error'));
                        buttons.forEach(btn => btn.disabled = false);
                    }}
                }} catch (error) {{
                    alert('Error performing action: ' + error.message);
                    buttons.forEach(btn => btn.disabled = false);
                }} finally {{
                    spinner.style.display = 'none';
                    btnText.style.display = 'block';
                }}
            }}

            function showDrawer(approved, text) {{
                const drawer = document.getElementById('drawer');
                const overlay = document.getElementById('drawer-overlay');
                const content = document.getElementById('drawer-content-box');
                
                content.innerHTML = `
                    <div style="display: flex; align-items: center; gap: 10px; margin-bottom: 20px;">
                        <div style="width: 12px; height: 12px; border-radius: 50%; background: ${{approved ? 'var(--success)' : 'var(--danger)'}}"></div>
                        <span style="font-weight: 600; text-transform: uppercase; font-size: 0.9rem; color: ${{approved ? 'var(--success)' : 'var(--danger)'}}">
                            ${{approved ? 'Approved' : 'Rejected'}}
                        </span>
                    </div>
                    <div class="result-block">${{text}}</div>
                `;
                
                drawer.classList.add('active');
                overlay.classList.add('active');
            }}

            function closeDrawer() {{
                const drawer = document.getElementById('drawer');
                const overlay = document.getElementById('drawer-overlay');
                drawer.classList.remove('active');
                overlay.classList.remove('active');
            }}

            window.onload = fetchPending;
        </script>
    </body>
    </html>
    """
    return HTMLResponse(content=html_content)


# ----------------- API Endpoints -----------------
@app.get("/api/pending")
async def get_pending_approvals():
    try:
        # List all sessions under the Agent Runtime ID
        logger.info(f"Listing sessions for reasoning engine: {AGENT_RUNTIME_ID}")
        sessions_resp = await session_service.list_sessions(app_name=AGENT_RUNTIME_ID, user_id=None)
        
        pending_approvals = []
        
        for sess in sessions_resp.sessions:
            logger.info(f"Fetching session: {sess.id} for user: {sess.user_id}")
            full_session = await session_service.get_session(
                app_name=AGENT_RUNTIME_ID,
                user_id=sess.user_id,
                session_id=sess.id
            )
            if not full_session:
                continue
                
            # Scan for unresolved adk_request_input events
            requests = {}
            responses = set()
            
            for event in full_session.events:
                if not event.content or not event.content.parts:
                    continue
                for part in event.content.parts:
                    # Detect input request
                    if part.function_call and part.function_call.name == "adk_request_input":
                        fc = part.function_call
                        interrupt_id = fc.id or (fc.args.get("interruptId") if fc.args else None)
                        if interrupt_id:
                            message = fc.args.get("message") if fc.args else ""
                            requests[interrupt_id] = {
                                "interrupt_id": interrupt_id,
                                "message": message,
                                "timestamp": event.timestamp
                            }
                    # Detect input response
                    elif part.function_response and part.function_response.name == "adk_request_input":
                        fr = part.function_response
                        if fr.id:
                            responses.add(fr.id)
                            
            # Any request that doesn't have a response is pending approval
            for interrupt_id, req in requests.items():
                if interrupt_id not in responses:
                    expense = full_session.state.get("expense", {})
                    risk_assessment = full_session.state.get("risk_assessment", {})
                    pending_approvals.append({
                        "session_id": sess.id,
                        "user_id": sess.user_id,
                        "interrupt_id": interrupt_id,
                        "message": req["message"],
                        "timestamp": req["timestamp"],
                        "expense": expense,
                        "risk_assessment": risk_assessment
                    })
                    
        return pending_approvals
    except Exception as e:
        logger.error(f"Error querying pending approvals: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/action/{session_id}")
async def resume_session(session_id: str, payload: ActionPayload):
    # Construct the resume message dict as requested
    message_payload = {
        "role": "user",
        "parts": [
            {
                "function_response": {
                    "name": "adk_request_input",
                    "response": {
                        "approved": "yes" if payload.approved else "no"
                    },
                    "id": payload.interrupt_id
                }
            }
        ]
    }
    
    try:
        engine = reasoning_engines.ReasoningEngine(AGENT_RUNTIME_ID)
        
        # Invoke stream query using execution_api_client to bypass registration issues
        response = stream_query_remote(
            engine,
            method_name="stream_query",
            message=message_payload,
            user_id="default-user", # Strictly set to default-user
            session_id=session_id
        )
        
        chunks = []
        outcome_text = ""
        
        for chunk in response:
            chunks.append(chunk)
            # Try to extract content text (e.g. final node output)
            if "content" in chunk and "parts" in chunk["content"]:
                for part in chunk["content"]["parts"]:
                    if "text" in part:
                        outcome_text += part["text"] + "\n"
                        
        return {
            "status": "success",
            "outcome_text": outcome_text.strip(),
            "raw_chunks": chunks
        }
    except Exception as e:
        logger.error(f"Error resuming session {session_id}: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))
