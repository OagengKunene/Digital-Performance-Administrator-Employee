import pytest
from pydantic import ValidationError
from pae.config import Settings


@pytest.mark.parametrize("value", [0, -1, 601])
def test_ai_deadline_must_be_positive_and_bounded(value):
    with pytest.raises(ValidationError):
        Settings(_env_file=None, ai_timeout_seconds=value)


@pytest.mark.parametrize("value", [0, -1, 51])
def test_model_call_budget_must_be_positive_and_bounded(value):
    with pytest.raises(ValidationError):
        Settings(_env_file=None, ai_max_model_calls=value)


@pytest.mark.parametrize("value", [0, -1, 61])
def test_request_rate_must_be_positive_and_bounded(value):
    with pytest.raises(ValidationError):
        Settings(_env_file=None, ai_requests_per_minute=value)
