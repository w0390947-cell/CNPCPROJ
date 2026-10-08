"""Run the synthetic retained-protection regressions using an installed package."""

import argparse
import json
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

import oilfield_energy
from oilfield_energy.control_contracts import PccSafetyConstraint
from oilfield_energy.execution_coordinator import ShanchengSafetyCoordinator
from oilfield_energy.shancheng_control import (
    CommandDisposition,
    DeviceStatus,
    ShanchengTelemetry,
    StorageTelemetry,
    SvgTelemetry,
    WindTurbineTelemetry,
)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    t = ShanchengTelemetry(
        snapshot_id="initial",
        observed_at_utc=datetime(2026, 1, 1, tzinfo=timezone.utc),
        elapsed_minutes=0,
        clock_hour=12,
        step_minutes=1,
        wind_turbines=(
            WindTurbineTelemetry("wind", 8, 0, 10, rated_active_power_mw=10),
        ),
        storage=StorageTelemetry(
            active_power_mw=0,
            energy_mwh=2.5,
            energy_min_mwh=0.5,
            energy_max_mwh=5,
            charge_power_max_mw=2.5,
            discharge_power_max_mw=2.5,
        ),
        svg=SvgTelemetry(reactive_power_mvar=0),
    )
    c = ShanchengSafetyCoordinator(source_epoch="installed-protection-audit")
    initial, _ = c.allocate(
        t,
        PccSafetyConstraint("hard", 0.15, "initial"),
        pcc_after_confirmed_pv_mw=-0.35,
        confirmed_pv_reduction_mw=0,
    )
    assert initial.command_accepted and c.active_target_cap_mw == 7.5
    rows = [c.last_target_record.model_dump(mode="json")]
    lower = replace(
        t,
        snapshot_id="lower",
        elapsed_minutes=1,
        observed_at_utc=t.observed_at_utc + timedelta(minutes=1),
        wind_turbines=(
            replace(t.wind_turbines[0], active_power_mw=1, available_active_power_mw=1),
        ),
        storage=replace(t.storage, charge_power_max_mw=0, discharge_power_max_mw=0),
    )
    d = c.enforce(lower, decision_id="renew-low")
    assert d.active_disposition is CommandDisposition.ACCEPTED
    assert d.active_target_mw == c.active_target_cap_mw == 1
    assert "P_TARGET_OUT_OF_RANGE" not in {a.code for a in d.alarms}
    rows.append(c.last_target_record.model_dump(mode="json"))
    fixed = replace(
        t,
        snapshot_id="fixed",
        elapsed_minutes=2,
        observed_at_utc=t.observed_at_utc + timedelta(minutes=2),
        wind_turbines=(
            replace(t.wind_turbines[0], status=DeviceStatus(remote_enabled=False)),
        ),
    )
    d = c.enforce(fixed, decision_id="renew-unattainable")
    assert (
        d.active_disposition is CommandDisposition.ACCEPTED
        and d.active_target_mw == 5.5
    )
    assert (
        c.active_target_cap_mw == 1
        and c.last_target_record.projection.capability_shortfall_mw == 4.5
    )
    rows.append(c.last_target_record.model_dump(mode="json"))
    fresh = ShanchengSafetyCoordinator(source_epoch="installed-shortfall-audit")
    allocation, _ = fresh.allocate(
        fixed,
        PccSafetyConstraint("hard", 0.15, "shortfall"),
        pcc_after_confirmed_pv_mw=-5.85,
        confirmed_pv_reduction_mw=0,
    )
    assert allocation.command_accepted and allocation.unserved_reduction_mw == 3.5
    assert not allocation.fully_allocated
    report = {
        "installed_path": str(Path(oilfield_energy.__file__).resolve()),
        "renewal_records": rows,
        "fresh_allocation_unserved_mw": allocation.unserved_reduction_mw,
        "scope": "command and allocation checks; no actual PCC trajectory is asserted",
    }
    if args.output:
        args.output.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(
        "Protection renewal, retained shortfall and separate PCC allocation checks passed."
    )


if __name__ == "__main__":
    main()
