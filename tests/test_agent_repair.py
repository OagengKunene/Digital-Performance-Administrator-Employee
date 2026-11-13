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
