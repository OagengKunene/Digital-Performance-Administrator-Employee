import asyncio
import json
from types import SimpleNamespace
import pytest
from pae.agents import run_reviewed_report


@pytest.mark.parametrize("always_wrong,expected_attempts,expected_status", [(False, 2, "verified_metrics"), (True, 3, "requires_review")])
def test_review_repairs_or_blocks_publication(monkeypatch, always_wrong, expected_attempts, expected_status):
    """SDK test double exercises host retry logic, not live model intelligence."""
    import google.adk.runners
    class RunnerDouble:
        attempts = 0
        def __init__(self, **kwargs):
            pass
        async def run_async(self, **kwargs):
            self.attempts += 1
            for name in ["reporting_agent", "quality_reviewer"]:
                part = SimpleNamespace(function_call=SimpleNamespace(name=name), function_response=None, thought=False)
                yield SimpleNamespace(content=SimpleNamespace(parts=[part]), author="coordinator", is_final_response=lambda: False)
            value = 56.25 if always_wrong or self.attempts == 1 else 75.0
            report = {"summary": "A report based on class evidence", "mean_percentage": value, "marked_count": 3, "missing_count": 1, "recommendations": []}
            part = SimpleNamespace(text=json.dumps(report), function_call=None, function_response=None, thought=False)
            yield SimpleNamespace(content=SimpleNamespace(parts=[part]), author="coordinator", is_final_response=lambda: True)
    monkeypatch.setattr(google.adk.runners, "Runner", RunnerDouble)
    result = asyncio.run(run_reviewed_report({"mean_percentage": 75.0, "marked_count": 3, "missing_count": 1}, "Review performance", "test-run"))
    reviews = [e for e in result["events"] if e["type"] == "review"]
    assert len(reviews) == expected_attempts
    assert reviews[0]["issues"]
    assert result["status"] == expected_status
    if not always_wrong:
        assert reviews[-1]["issues"] == []
        assert result["result"]["mean_percentage"] == 75


def test_timeout_cancels_work_and_retains_completed_tool(monkeypatch):
    """A short test deadline cancels a genuinely stalled async generator."""
    import google.adk.runners
    cancelled = []

    class SlowRunner:
        def __init__(self, **kwargs):
            pass

        async def run_async(self, **kwargs):
            try:
                part = SimpleNamespace(function_call=None,
                    function_response=SimpleNamespace(name="get_class_evidence"), thought=False)
                yield SimpleNamespace(content=SimpleNamespace(parts=[part]),
                    author="coordinator", is_final_response=lambda: False)
                await asyncio.sleep(10)
            finally:
                cancelled.append(True)

    monkeypatch.setattr("pae.agents.settings", lambda: SimpleNamespace(
        google_api_key="", ai_timeout_seconds=0.1, ai_max_model_calls=24))
    monkeypatch.setattr("pae.agents.build_team", lambda data, **kwargs: object())
    monkeypatch.setattr(google.adk.runners, "Runner", SlowRunner)
    result = asyncio.run(run_reviewed_report(
        {"mean_percentage": 75.0, "marked_count": 3, "missing_count": 1},
        "Review performance", "timeout-test"))
    assert result["status"] == "failed"
    assert result["result"]["error_code"] == "workflow_timeout"
    assert result["result"]["limit_seconds"] == 0.1
    assert result["result"]["last_completed_tool"] == "get_class_evidence"
    assert result["result"]["elapsed_seconds"] >= 0.09
    assert result["events"][0]["elapsed_seconds"] >= 0
    assert cancelled == [True]
