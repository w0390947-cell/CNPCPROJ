from datetime import datetime, timezone
from pathlib import Path

import pytest
from pydantic import ValidationError

from oilfield_energy.modules.dispatch.api import (
    adopt_first_steps,
    read_adopted_schedule,
    slice_adopted_schedule,
)
from oilfield_energy.modules.dispatch.contracts import (
    AdoptedOrigin,
    AdoptedSchedule,
    FirstStepDecision,
    LegacyAdoptedScheduleRecord,
    ResourceTrajectory,
)


def decision(index, *, identity="svg", bus="B", stop=None):
    return FirstStepDecision(
        origin=AdoptedOrigin(
            interval_index=index,
            window_start_interval=index,
            window_end_interval=stop or index + 4,
        ),
        p_grid_mw=float(index + 1),
        q_grid_mvar=0.1,
        wind_available_mw=2.0,
        resources=(
            ResourceTrajectory(
                resource_id=identity,
                bus_id=bus,
                resource_type="svg",
                active_power_mw=(0.0,),
                reactive_power_mvar=(float(index) / 10,),
            ),
        ),
    )


@pytest.mark.parametrize("count,horizon", [(8, 4), (96, 16)])
def test_adopted_horizon_has_per_window_provenance_not_solver_certificate(count, horizon):
    start = datetime(2026, 9, 18, tzinfo=timezone.utc)
    schedule = adopt_first_steps(
        tuple(decision(i, stop=min(count, i + horizon)) for i in range(count)),
        microgrid_id="SC",
        start=start,
        step_minutes=15,
    )
    assert len(schedule.p_grid_mw) == count
    assert schedule.p_grid_mw[-1] == count
    assert schedule.origins[-1].window_end_interval == count
    assert schedule.objective_cny is None
    assert not {"cluster", "mip_gap", "success", "model_size"} & schedule.model_dump().keys()
    assert read_adopted_schedule(schedule.model_dump_json()) == schedule
    sliced = slice_adopted_schedule(schedule, 2, 5)
    assert sliced.start.timestamp() - start.timestamp() == 30 * 60
    assert [o.interval_index for o in sliced.origins] == [2, 3, 4]
    assert sliced.resource_schedules[0].reactive_power_mvar == (0.2, 0.3, 0.4)


@pytest.mark.parametrize(
    "decisions",
    [
        (),
        (decision(0), decision(2)),
        (decision(0), decision(1, identity="other")),
        (decision(0), decision(1, bus="other")),
    ],
)
def test_rejects_incomplete_or_changed_resource_lineage(decisions):
    with pytest.raises(ValueError):
        adopt_first_steps(
            decisions,
            microgrid_id="SC",
            start=datetime(2026, 9, 18, tzinfo=timezone.utc),
            step_minutes=15,
        )


def test_historical_archive_remains_readable_without_invented_certificate_scope():
    text = '{"success":true,"status":0,"message":"old","objective_cny":10.0,"solver_objective_without_constants_cny":9.0,"mip_gap":0.001,"microgrids":{"SC":{"p_grid_mw":[1.0,2.0]}},"cluster":{"total_import_mw":[1.0]},"model_size":{"variables":1}}'
    old = read_adopted_schedule(text)
    assert isinstance(old, LegacyAdoptedScheduleRecord)
    assert old.objective_cny == 10.0
    assert old.cluster["total_import_mw"] == [1.0]
    assert not isinstance(old, AdoptedSchedule)
    with pytest.raises(ValidationError):
        read_adopted_schedule('{"schema_version":"adopted-schedule-v999"}')


def test_real_pre_fix_archive_read_preserves_historical_scope():
    path = Path(__file__).resolve().parents[2] / "fixtures/studies/legacy_adopted_schedule.json"
    old = read_adopted_schedule(path.read_text(encoding="utf-8"))
    assert isinstance(old, LegacyAdoptedScheduleRecord)
    assert len(old.microgrids["SC"]["p_grid_mw"]) == 8
    assert len(old.cluster["total_import_mw"]) == 4
