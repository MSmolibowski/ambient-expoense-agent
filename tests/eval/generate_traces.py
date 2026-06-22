import os
import json
import uuid
import asyncio
import pathlib
from typing import Any, Optional

import vertexai
from google.genai import types
from google.adk.runners import Runner
from google.adk.cli.utils.service_factory import (
    create_session_service_from_options,
    create_artifact_service_from_options,
)
from google.adk.utils.context_utils import Aclosing

from expense_agent.agent import app as adk_app

# Load Vertex AI SDK types for trace output schema
from vertexai._genai.types.common import EvaluationDataset, EvalCase, ResponseCandidate
from vertexai._genai.types.evals import AgentData, ConversationTurn, Event as EvalEvent

async def run_case(case: dict, runner: Runner, session_service: Any) -> EvalCase:
    case_id = case["eval_case_id"]
    prompt_content = case["prompt"]
    prompt_text = prompt_content["parts"][0]["text"]
    
    # Check if this case contains prompt injection keywords
    injection_triggers = [
        "ignore", "bypass", "override", "system prompt", "instead of",
        "instruction", "auto-approve", "auto approve", "force approval",
        "you are now", "new rule", "disable rules"
    ]
    is_injection = any(trigger in prompt_text.lower() for trigger in injection_triggers)
    
    session_id = f"eval-{case_id}-{uuid.uuid4()}"
    user_id = "eval-user"
    
    # 1. Create the session
    await session_service.create_session(
        app_name=adk_app.name,
        user_id=user_id,
        session_id=session_id
    )
    
    new_message = types.Content.model_validate(prompt_content)
    
    all_events = []
    
    # 2. Run the first execution
    interrupted = False
    interrupt_id = None
    
    async with Aclosing(
        runner.run_async(
            user_id=user_id,
            session_id=session_id,
            new_message=new_message
        )
    ) as agen:
        async for event in agen:
            all_events.append(event)
            # Check if there is an interrupt function call
            if event.content and event.content.parts:
                for part in event.content.parts:
                    fc = part.function_call
                    if fc and fc.name == "adk_request_input":
                        interrupted = True
                        interrupt_id = fc.id
                        break
                        
    # 3. If interrupted, send automatic response
    if interrupted and interrupt_id:
        decision = "no" if is_injection else "yes"
        print(f"[{case_id}] Workflow paused for manager approval. Decision auto-escalated to: {decision}")
        
        resume_message = types.Content(
            role="user",
            parts=[
                types.Part(
                    function_response=types.FunctionResponse(
                        id=interrupt_id,
                        name="adk_request_input",
                        response={"result": decision}
                    )
                )
            ]
        )
        
        async with Aclosing(
            runner.run_async(
                user_id=user_id,
                session_id=session_id,
                new_message=resume_message
            )
        ) as agen:
            async for event in agen:
                all_events.append(event)
                
    # 4. Load full session history from DB
    session = await session_service.get_session(
        app_name=adk_app.name,
        user_id=user_id,
        session_id=session_id
    )
    
    events_from_db = session.events if session else []
    
    # Convert DB events to EvalEvents
    eval_events = []
    for event in events_from_db:
        if not event.content or not event.content.parts:
            continue
            
        content = event.content
        has_valid_part = False
        for part in content.parts:
            if part.text or part.function_call or part.function_response:
                has_valid_part = True
            if hasattr(part, "thought_signature") and part.thought_signature:
                part.thought_signature = None
                
        if not has_valid_part:
            continue
            
        # Author should be either 'user' or the root agent's name
        author = event.author
        if author == "model":
            author = "root_agent"
            
        eval_events.append(
            EvalEvent(
                author=author,
                content=content,
                creation_timestamp=event.timestamp,
                event_id=event.id
            )
        )
        
    # Extract final text response
    final_text = ""
    for event in reversed(events_from_db):
        if event.content and event.content.parts:
            texts = [p.text for p in event.content.parts if p.text]
            if texts:
                final_text = "".join(texts).strip()
                break
                
    response_cand = ResponseCandidate(
        response=types.Content(
            role="model",
            parts=[types.Part(text=final_text)]
        )
    )
    
    eval_case = EvalCase(
        eval_case_id=case_id,
        prompt=new_message,
        agent_data=AgentData(
            turns=[
                ConversationTurn(
                    turn_index=0,
                    turn_id="turn_0",
                    events=eval_events
                )
            ]
        ),
        responses=[response_cand]
    )
    
    return eval_case

async def main():
    dataset_path = "tests/eval/datasets/basic-dataset.json"
    output_path = "artifacts/traces/generated_traces.json"
    
    print(f"Loading basic dataset from {dataset_path}...")
    with open(dataset_path, "r", encoding="utf-8") as f:
        data = json.load(f)
        
    eval_cases = data["eval_cases"]
    
    # Initialize services
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
    
    eval_case_results = []
    for case in eval_cases:
        print(f"Running inference for case: {case['eval_case_id']}...")
        result_case = await run_case(case, runner, session_service)
        eval_case_results.append(result_case)
        print(f"Finished case: {case['eval_case_id']}")
        
    dataset = EvaluationDataset(eval_cases=eval_case_results)
    
    # Write to output_path
    output_file = pathlib.Path(output_path)
    output_file.parent.mkdir(parents=True, exist_ok=True)
    output_file.write_text(dataset.model_dump_json(indent=2, exclude_none=True), encoding="utf-8")
    print(f"Populated traces successfully written to: {output_path}")

if __name__ == "__main__":
    asyncio.run(main())
