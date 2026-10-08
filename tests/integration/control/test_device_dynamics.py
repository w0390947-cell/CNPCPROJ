"""Exercise public simulation paths, not only the standalone ramp projection."""

import pytest

from oilfield_energy.bootstrap.adapters.cluster_execution import execution_inputs
from oilfield_energy.hierarchy_types import TimeScaleConfig
from oilfield_energy.service import ScenarioType, SimulationRequest
from tests.legacy_case_fixture import build_synthetic_case


def test_unrepresentable_grid_is_rejected_before_optimization_or_schedule_access():
    case = build_synthetic_case(steps=7)
    with pytest.raises(ValueError, match="integral planning intervals"):
        execution_inputs(case, packaged=False, events=[])


def test_planning_only_service_still_accepts_non_minute_grid():
    request = SimulationRequest(
        scenario_type=ScenarioType.CLUSTER_COORDINATION, steps=7
    )
    assert request.steps == 7


@pytest.mark.parametrize(
    "changes",
    [
        dict(device_step_minutes=1.5),
        dict(device_step_minutes=True),
        dict(device_step_minutes=0),
        dict(active_power_ramp_mw_per_minute=-1),
        dict(active_power_ramp_mw_per_minute=float("nan")),
        dict(device_time_constant_minutes=float("inf")),
    ],
)
def test_invalid_dynamic_configuration_is_rejected(changes):
    with pytest.raises(ValueError):
        TimeScaleConfig(**changes)
