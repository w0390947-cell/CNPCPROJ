"""Reproduce item06 control boundaries through installed public interfaces."""

from __future__ import annotations

import argparse
import hashlib
import json
from dataclasses import asdict, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

import oilfield_energy.shancheng_control as control

BASE = datetime(2026, 1, 1, tzinfo=timezone.utc)


def snapshot(
    *,
    elapsed: float = 0.0,
    hour: float = 0.0,
    step: float = 1.0,
    wind_p: float = 4.0,
    wind_available: float = 5.0,
    wind_q: float = 0.0,
    energy: float = 5.0,
) -> control.ShanchengTelemetry:
    return control.ShanchengTelemetry(
        snapshot_id=f"audit06:{elapsed}:{hour}",
        observed_at_utc=BASE + timedelta(minutes=elapsed),
        elapsed_minutes=elapsed,
        clock_hour=hour,
        step_minutes=step,
        wind_turbines=tuple(
            control.WindTurbineTelemetry(
                name=f"WT{index}",
                active_power_mw=wind_p,
                reactive_power_mvar=wind_q,
                available_active_power_mw=wind_available,
            )
            for index in (1, 2)
        ),
        storage=control.StorageTelemetry(
            active_power_mw=0.0,
            energy_mwh=energy,
            energy_min_mwh=0.5,
            energy_max_mwh=5.0,
            charge_power_max_mw=2.5,
            discharge_power_max_mw=2.5,
        ),
        svg=control.SvgTelemetry(reactive_power_mvar=1.5),
    )


def dispatch_case(available: float) -> dict:
    controller = control.ShanchengController()
    factory = control.LegacyCommandFactory(source_id="audit06", source_epoch="fixed")
    factory.authorize(controller)
    first = snapshot()
    command = factory.make(
        now_utc=first.observed_at_utc, message_id="audit06:p6", active_target_mw=6.0
    )
    accepted = controller.step(first, command)
    after = snapshot(elapsed=1.0, wind_p=3.0, wind_available=available)
    tracked = controller.step(after)
    physical_max = sum(w.available_active_power_mw for w in after.wind_turbines)
    excess = [
        max(0.0, point.active_power_mw - wind.available_active_power_mw)
        if point.active_power_mw is not None
        else 0.0
        for point, wind in zip(tracked.setpoints.wind, after.wind_turbines, strict=True)
    ]
    return {
        "initial_snapshot": asdict(first),
        "accepted": asdict(accepted),
        "after_snapshot": asdict(after),
        "tracked": asdict(tracked),
        "independent_max_target_mw_no_discharge": physical_max,
        "wind_command_excess_mw": excess,
        "violates_available_power": any(value > 1e-9 for value in excess),
    }


def joint_case(available: float) -> dict:
    telemetry = snapshot(wind_available=available, wind_q=1.2)
    decision = control.ShanchengController().step(telemetry)
    excess = []
    for point, wind in zip(decision.setpoints.wind, telemetry.wind_turbines, strict=True):
        effective_p = (
            min(wind.active_power_mw, point.active_power_mw)
            if point.active_power_mw is not None
            else wind.active_power_mw
        )
        effective_q = (
            point.reactive_power_mvar
            if point.reactive_power_mvar is not None
            else wind.reactive_power_mvar
        )
        excess.append(max(0.0, abs(effective_q) - 0.3 * effective_p))
    return {
        "snapshot": asdict(telemetry),
        "decision": asdict(decision),
        "transition_envelope_excess_mvar": excess,
        "unsafe_active_write": any(
            value > 1e-9 and point.active_power_mw is not None
            for value, point in zip(excess, decision.setpoints.wind, strict=True)
        ),
    }


def energy_case(*, hour: float, step: float, energy: float) -> dict:
    telemetry = snapshot(hour=hour, step=step, energy=energy)
    decision = control.ShanchengController().step(telemetry)
    power = decision.setpoints.storage_active_power_mw
    assert power is not None
    # Independently integrate one declared zero-order-hold interval.
    after = energy - power * step / 60.0 / telemetry.storage.discharge_efficiency
    safe_max = min(2.5, (energy - 0.5) * 0.95 / (step / 60.0))
    return {
        "snapshot": asdict(telemetry),
        "decision": asdict(decision),
        "next_energy_mwh": after,
        "independent_one_step_discharge_max_mw": safe_max,
        "energy_below_minimum_mwh": max(0.0, 0.5 - after),
        "violates_energy_bound": after < 0.5 - 1e-9,
    }


def interlock_cases() -> list[dict]:
    results = []
    for position in range(25):
        bms = tuple(index == position for index in range(12))
        pcs = tuple(index + 12 == position for index in range(12))
        locks = control.StorageInterlocks(bms, pcs, position == 24)
        telemetry = snapshot(hour=12.0, energy=2.5)
        telemetry = replace(telemetry, storage=replace(telemetry.storage, interlocks=locks))
        decision = control.ShanchengController().step(telemetry)
        results.append(
            {
                "position": position,
                "active_write": decision.setpoints.storage_active_power_mw,
                "reactive_write": decision.setpoints.storage_reactive_power_mvar,
                "charge_capability_mw": decision.capabilities.storage_charge_capability_mw,
                "discharge_capability_mw": decision.capabilities.storage_discharge_capability_mw,
            }
        )
    return results


def availability_sweep() -> dict:
    checked = 0
    normal_checked = 0
    mismatches = []
    for measured in range(6):
        for available in range(6):
            for target in range(11):
                telemetry = snapshot(wind_p=float(measured), wind_available=float(available))
                controller = control.ShanchengController()
                factory = control.LegacyCommandFactory(
                    source_id="audit06-grid", source_epoch="fixed"
                )
                factory.authorize(controller)
                command = factory.make(
                    now_utc=telemetry.observed_at_utc,
                    message_id="audit06-grid:p",
                    active_target_mw=float(target),
                )
                decision = controller.step(telemetry, command)
                accepted = decision.active_disposition is control.CommandDisposition.ACCEPTED
                # Both units controllable, Q=0, full battery, discharge disabled.
                feasible = 0 <= target <= 2 * available
                checked += 1
                normal_checked += int(measured <= available)
                if accepted != feasible:
                    mismatches.append(
                        {
                            "measured_per_turbine_mw": measured,
                            "available_per_turbine_mw": available,
                            "target_mw": target,
                            "expected_feasible": feasible,
                            "accepted": accepted,
                            "decision": asdict(decision),
                        }
                    )
    return {
        "checked": checked,
        "normal_measured_at_or_below_available_checked": normal_checked,
        "mismatch_count": len(mismatches),
        "mismatches": mismatches,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--source-root", type=Path, required=True)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=False)
    cases = {
        "dispatch_availability_drop": dispatch_case(1.0),
        "dispatch_normal_control": dispatch_case(5.0),
        "local_joint_violation": joint_case(1.0),
        "local_joint_normal_control": joint_case(5.0),
        "local_discharge_crossing_1min": energy_case(hour=22 + 59.5 / 60, step=1, energy=0.51),
        "local_discharge_crossing_15min": energy_case(hour=22 + 55 / 60, step=15, energy=0.6),
        "local_discharge_aligned_control": energy_case(hour=22 + 59 / 60, step=1, energy=0.51),
    }
    # Configured interval and phase sweep, not a random frequency estimate.
    sweep = []
    for step in (1.0, 5.0, 15.0):
        for remaining_fraction in (0.25, 0.5, 1.0, 2.0):
            case = energy_case(hour=23.0 - step * remaining_fraction / 60.0, step=step, energy=0.51)
            sweep.append(
                {
                    "step_minutes": step,
                    "remaining_window_minutes": step * remaining_fraction,
                    "violates_energy_bound": case["violates_energy_bound"],
                    "next_energy_mwh": case["next_energy_mwh"],
                }
            )
    cases["energy_boundary_sweep"] = sweep
    cases["all_25_individual_interlocks"] = interlock_cases()
    cases["new_dispatch_availability_sweep"] = availability_sweep()
    root = args.source_root.resolve()
    manifest = {
        str(path.relative_to(root)): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted((root / "src/oilfield_energy").rglob("*.py"))
    }
    old = json.loads(
        (root / "artifacts/audit_20260917/item05/source_manifest_after_fix.json").read_text(
            encoding="utf-8"
        )
    )
    integrity = {
        "imported_controller": str(Path(control.__file__).resolve()),
        "imported_controller_sha256": hashlib.sha256(
            Path(control.__file__).read_bytes()
        ).hexdigest(),
        "source_file_count": len(manifest),
        "changed_since_item05": [key for key in manifest if old.get(key) != manifest[key]],
        "removed_since_item05": sorted(set(old) - set(manifest)),
        "source_manifest": manifest,
    }
    for name, value in (("reproductions", cases), ("audit_integrity", integrity)):
        (args.output / f"{name}.json").write_text(
            json.dumps(value, indent=2, ensure_ascii=True, default=str, allow_nan=False) + "\n",
            encoding="utf-8",
        )
    print(
        json.dumps(
            {
                "availability_defect": cases["dispatch_availability_drop"][
                    "violates_available_power"
                ],
                "joint_defect": cases["local_joint_violation"]["unsafe_active_write"],
                "energy_defect": cases["local_discharge_crossing_1min"]["violates_energy_bound"],
                "negative_controls_pass": not any(
                    (
                        cases["dispatch_normal_control"]["violates_available_power"],
                        cases["local_joint_normal_control"]["unsafe_active_write"],
                        cases["local_discharge_aligned_control"]["violates_energy_bound"],
                    )
                ),
                "source_unchanged": manifest == old,
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
