import asyncio
import json
from types import SimpleNamespace
import pytest
from google.genai import types
from google.adk.models.google_llm import Gemini
from google.adk.models.llm_response import LlmResponse
from pae.agents import run_stage, StageOutputInvalid, SCHEMAS
from test_agent_repair import output


class Gate:
    async def wait(self):
        return 0


@pytest.mark.parametrize("stage", list(SCHEMAS))
def test_actual_adk_stage_uses_one_inference_and_excludes_thoughts(monkeypatch, ai_config, stage):
    seen = []
    class ModelDouble(Gemini):
        async def generate_content_async(self, llm_request, stream=False):
            seen.append(llm_request)
            assert "{not_a_state_key}" in llm_request.config.system_instruction
            yield LlmResponse(content=types.Content(role="model", parts=[
                types.Part(text="private model reasoning", thought=True),
                types.Part.from_text(text=json.dumps(output(stage)))]))
    monkeypatch.setattr("pae.agents.make_model", lambda *args: ModelDouble(model="gemini-3.6-flash"))
    monkeypatch.setattr("pae.agents.model_pacer", lambda *args, **kwargs: Gate())
    requests = []
    async def track(event):
        requests.append(event)
    result = asyncio.run(run_stage(stage, {"class_name": "{not_a_state_key}",
        "mean_percentage": 75.0, "marked_count": 3, "missing_count": 1},
        "Review recorded performance.", {}, "gemini", "gemini-3.6-flash", "stage-test", track))
    assert result == output(stage)
    assert len(seen) == len(requests) == 1
    assert "private model reasoning" not in json.dumps(result)


def test_groq_adapter_runs_through_actual_adk_litellm_connector(monkeypatch, ai_config):
    from litellm import ModelResponse
    from google.adk.models.lite_llm import LiteLLMClient
    monkeypatch.setattr(ai_config, "groq_api_key", "private-fake-key")
    monkeypatch.setattr("pae.agents.model_pacer", lambda *args, **kwargs: Gate())
    calls = []
    async def answer(self, **kwargs):
        calls.append(kwargs)
        return ModelResponse(model=kwargs["model"], choices=[{
            "index": 0, "finish_reason": "stop",
            "message": {"role": "assistant", "content": json.dumps(output("coordinator"))}}],
            usage={"prompt_tokens": 10, "completion_tokens": 20, "total_tokens": 30})
    monkeypatch.setattr(LiteLLMClient, "acompletion", answer)
    result = asyncio.run(run_stage("coordinator", {"mean_percentage": 75.0,
        "marked_count": 3, "missing_count": 1}, "Review class performance.", {},
        "groq", "openai/gpt-oss-20b", "groq-stage-test"))
    assert result == output("coordinator")
    assert len(calls) == 1
    assert calls[0]["model"] == "groq/openai/gpt-oss-20b"
    assert calls[0]["num_retries"] == 0
    assert "private-fake-key" not in json.dumps(result)


def test_bad_report_metrics_are_blocked_at_stage_boundary(monkeypatch, ai_config):
    class WrongModel(Gemini):
        async def generate_content_async(self, llm_request, stream=False):
            result = output("reporting_agent")
            result["mean_percentage"] = 56.25
            yield LlmResponse(content=types.Content(role="model",
                parts=[types.Part.from_text(text=json.dumps(result))]))
    monkeypatch.setattr("pae.agents.make_model", lambda *args: WrongModel(model="gemini-3.6-flash"))
    monkeypatch.setattr("pae.agents.model_pacer", lambda *args, **kwargs: Gate())
    with pytest.raises(StageOutputInvalid):
        asyncio.run(run_stage("reporting_agent", {"mean_percentage": 75.0,
            "marked_count": 3, "missing_count": 1}, "Review class.", {},
            "gemini", "gemini-3.6-flash", "invalid-stage-test"))


def test_stage_deadline_cancels_a_stalled_model(monkeypatch, ai_config):
    monkeypatch.setattr(ai_config, "ai_timeout_seconds", 0.1)
    cancelled = []
    class SlowModel(Gemini):
        async def generate_content_async(self, llm_request, stream=False):
            try:
                await asyncio.sleep(10)
                yield LlmResponse()
            finally:
                cancelled.append(True)
    monkeypatch.setattr("pae.agents.make_model", lambda *args: SlowModel(model="gemini-3.6-flash"))
    monkeypatch.setattr("pae.agents.model_pacer", lambda *args, **kwargs: Gate())
    with pytest.raises(TimeoutError):
        asyncio.run(run_stage("coordinator", {}, "Review class.", {},
            "gemini", "gemini-3.6-flash", "timeout-stage-test"))
    assert cancelled == [True]


def test_full_adk_groq_job_recovers_reporting_stage_from_database(
        client, teacher, ai_config, db_factory, monkeypatch):
    from litellm import ModelResponse
    from google.adk.models.lite_llm import LiteLLMClient
    from pae.agents import ROLES
    from pae.jobs import process_one
    from pae.db import AgentJob
    monkeypatch.setattr(ai_config, "ai_provider", "groq")
    monkeypatch.setattr(ai_config, "groq_api_key", "fake-private-key")
    monkeypatch.setattr("pae.agents.model_pacer", lambda *args, **kwargs: Gate())
    stages = []

    async def answer(self, **kwargs):
        messages = "\n".join(str(message.get("content", "")) for message in kwargs["messages"])
        stage = next(name for name, instruction in ROLES.items() if instruction in messages)
        stages.append(stage)
        if stage == "reporting_agent" and stages.count(stage) == 1:
            error = RuntimeError("private provider error")
            error.status_code = 503
            raise error
        return ModelResponse(model=kwargs["model"], choices=[{
            "index": 0, "finish_reason": "stop",
            "message": {"role": "assistant", "content": json.dumps(output(stage))}}],
            usage={"prompt_tokens": 10, "completion_tokens": 20, "total_tokens": 30})
    monkeypatch.setattr(LiteLLMClient, "acompletion", answer)
    queued = client.post("/classes/1/runs", headers=teacher,
        json={"goal": "Investigate class evidence and prepare a teacher report."})
    assert queued.status_code == 202
    identifier = queued.json()["id"]
    for _ in range(4):
        asyncio.run(process_one(db_factory))
    failed = client.get("/classes/1/runs/" + identifier, headers=teacher).json()
    assert failed["status"] == "retrying", failed
    with db_factory() as db:
        db.get(AgentJob, identifier).next_at = 0
        db.commit()
    for _ in range(2):
        asyncio.run(process_one(db_factory))
    result = client.get("/classes/1/runs/" + identifier, headers=teacher).json()
    assert result["status"] == "verified_metrics", result
    assert stages == ["coordinator", "performance_analyst", "intervention_planner",
                      "reporting_agent", "reporting_agent", "quality_reviewer"]
