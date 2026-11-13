import asyncio
import json
from types import SimpleNamespace
import pytest

from pae.rate_limit import RequestPacer
from pae.agents import run_reviewed_report


class Clock:
    now = 0

    def time(self):
        return self.now

    async def sleep(self, seconds):
        self.now += seconds
        await asyncio.sleep(0)


def test_pacing_includes_initial_cooldown_and_spaces_all_requests():
    clock = Clock()
    pacer = RequestPacer(4, clock=clock.time, sleep=clock.sleep)

    async def calls():
        starts = []
        for _ in range(9):
            await pacer.wait()
            starts.append(clock.now)
        return starts

    starts = asyncio.run(calls())
    assert starts[0] >= 60
    assert all(b - a > 15 for a, b in zip(starts, starts[1:]))
    assert all(sum(start <= other < start + 60 for other in starts) <= 4
               for start in starts)


def test_concurrent_waiters_do_not_burst_together():
    import time
    pacer = RequestPacer(4, initial_delay=0, period_seconds=0.08)

    async def calls():
        async def one():
            await pacer.wait()
            return time.monotonic()
        return sorted(await asyncio.gather(*(one() for _ in range(6))))

    starts = asyncio.run(calls())
    assert all(b - a >= 0.019 for a, b in zip(starts, starts[1:]))


@pytest.mark.parametrize("budget,expected_status", [(10, "verified_metrics"), (3, "requires_review")])
def test_actual_adk_delegates_and_paces_every_model_request(monkeypatch, budget, expected_status):
    """Real ADK Runner and AgentTools, with only model inference replaced."""
    from pydantic import PrivateAttr
    from google.adk.models.google_llm import Gemini
    from google.adk.models.llm_response import LlmResponse
    from google.genai import types

    clock = Clock()
    pacer = RequestPacer(4, clock=clock.time, sleep=clock.sleep)
    monkeypatch.setattr("pae.agents.model_pacer", lambda *args: pacer)
    monkeypatch.setattr("pae.agents.settings", lambda: SimpleNamespace(
        google_api_key="", gemini_model="gemini-3.6-flash", ai_timeout_seconds=20,
        ai_max_model_calls=budget, ai_requests_per_minute=4))
    seen = []

    class ModelDouble(Gemini):
        _coordinator_calls: int = PrivateAttr(default=0)

        async def generate_content_async(self, llm_request, stream=False):
            seen.append(clock.now)
            assert "{not_a_state_key}" in str(llm_request.config.system_instruction)
            if "performance_analyst" in llm_request.tools_dict:
                names = ["performance_analyst", "intervention_planner", "reporting_agent", "quality_reviewer"]
                index = self._coordinator_calls
                self._coordinator_calls += 1
                if index < len(names):
                    part = types.Part.from_function_call(name=names[index], args={"request": "Use the supplied evidence; review the full draft: sample report."})
                else:
                    part = types.Part.from_text(text=json.dumps({
                        "summary": "Source-grounded report for staff review", "mean_percentage": 75.0,
                        "marked_count": 3, "missing_count": 1, "recommendations": [],
                    }))
            else:
                part = types.Part.from_text(text="PASS. Sample analysis and recommendations based on supplied evidence.")
            yield LlmResponse(content=types.Content(role="model", parts=[part]))

    monkeypatch.setattr("google.adk.models.google_llm.Gemini", ModelDouble)
    result = asyncio.run(run_reviewed_report({"mean_percentage": 75.0,
        "marked_count": 3, "missing_count": 1, "name": "Learner {not_a_state_key}"},
        "Review performance", "paced-sdk-test"))
    assert result["status"] == expected_status, result
    assert len(seen) <= budget
    model_events = [event for event in result["events"] if event["type"] == "model_request"]
    assert len(model_events) == len(seen)
    assert all(b - a > 15 for a, b in zip(seen, seen[1:]))
    if budget == 10:
        assert len(seen) == 9
        assert {event["agent"] for event in model_events} == {
            "performance_administrator", "performance_analyst", "intervention_planner",
            "reporting_agent", "quality_reviewer"}
    else:
        assert result["result"]["error_code"] == "model_call_budget"
