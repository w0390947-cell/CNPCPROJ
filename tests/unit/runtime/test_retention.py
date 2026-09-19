"""Deterministic retention boundaries, independent of files and wall clocks."""

from datetime import datetime, timedelta, timezone

import pytest
from pydantic import ValidationError

from oilfield_energy.runtime.retention import RetentionPolicy

NOW = datetime(2026, 9, 19, tzinfo=timezone.utc)


def test_defaults_and_exact_time_capacity_boundaries():
    policy = RetentionPolicy()
    assert policy.max_bytes == 5_000_000_000
    assert policy.reason(NOW - timedelta(days=1), NOW, 0) == "expired"
    assert policy.reason(NOW - timedelta(days=1, microseconds=-1), NOW, 0) is None
    assert policy.reason(NOW, NOW, 5_000_000_000) is None
    assert policy.reason(NOW, NOW, 5_000_000_001) == "capacity"
    assert policy.reason(NOW + timedelta(hours=1), NOW, 0) is None


def test_timezones_compare_instants_and_naive_dates_fail():
    policy = RetentionPolicy()
    end = (NOW - timedelta(days=1)).astimezone(timezone(timedelta(hours=8)))
    assert policy.reason(end, NOW, 0) == "expired"
    with pytest.raises(ValueError, match="timezone"):
        policy.reason(NOW.replace(tzinfo=None), NOW, 0)


@pytest.mark.parametrize(
    "values",
    [
        {"max_bytes": 0},
        {"max_age_seconds": -1},
        {"interval_seconds": 0},
        {"batch_size": 0},
        {"max_bytes": "5000"},
        {"max_bytes": True},
        {"unknown": 1},
    ],
)
def test_invalid_policy_fails_at_configuration_boundary(values):
    with pytest.raises(ValidationError):
        RetentionPolicy(**values)
