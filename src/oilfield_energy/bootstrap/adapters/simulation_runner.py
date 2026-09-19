"""Compatibility implementation of the study port using existing numerical APIs.

Legacy result dictionaries are intentionally contained here; all crossing inputs
are validated contracts. No solver failure is rewritten into a successful check.
"""
# pyright: reportUnknownMemberType=false, reportUnknownArgumentType=false, reportUnknownVariableType=false

import dataclasses
import json
import math
from datetime import datetime, timedelta
from enum import Enum
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any, cast

import numpy as np
from pydantic import BaseModel

from oilfield_energy.ac_consistency import solve_case_ac_consistent
from oilfield_energy.device_control import simulate_device_tracking
from oilfield_energy.field_data.snapshot_power_flow import (
    build_field_snapshot,
    calculate_field_power_flow,
    read_snapshot_request,
)
from oilfield_energy.group_control_scenarios import run_group_control_scenarios
from oilfield_energy.hierarchy_reporting import write_minute_network_outputs
from oilfield_energy.modules.control.contracts import BusSeries, PlantInputs
from oilfield_energy.modules.measurements.contracts import CapturedDataset
from oilfield_energy.modules.studies.contracts import ScenarioCatalog
from oilfield_energy.planning_security import build_planning_security_trajectories
from oilfield_energy.power_flow_comparison import (
    ComparisonRequest,
    DevicePerturbation,
    FixedStateSnapshot,
    compare_fixed_states,
)
from oilfield_energy.workflows.field_dataset.adapters.document_assembly import (
    assemble_request,
)
from oilfield_energy.workflows.field_dataset.api import dataset_artifacts
from oilfield_energy.workflows.field_dataset.contracts import Artifact
from oilfield_energy.workflows.shancheng_simulation.contracts import StudyEvaluation

from .simulation_case import join_first_steps, make_case, slice_schedule
from .simulation_files import captured_content, load_study


def normalized(value: Any) -> Any:
    """Archive unknown numerical outputs as null, retaining explicit validity."""
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return normalized(dataclasses.asdict(value))
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, np.ndarray):
        return normalized(value.tolist())
    if isinstance(value, np.generic):
        return normalized(value.item())
    if isinstance(value, dict):
        return {str(k): normalized(v) for k, v in value.items()}
    if isinstance(value, (tuple, list)):
        return [normalized(v) for v in value]
    if isinstance(value, float) and not math.isfinite(value):
        return None
    return value


def save(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            normalized(value),
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
        ),
        encoding="utf-8",
    )


class SimulationEngine:
    def evaluate(self, dataset: CapturedDataset) -> StudyEvaluation:
        checks: dict[str, bool] = {}
        with TemporaryDirectory(prefix="SC-study-") as directory:
            root = Path(directory)
            output = root / "results"
            output.mkdir()
            try:
                self._run(dataset, root, output, checks)
            except (ValueError, RuntimeError, OSError) as exc:
                checks["execution_completed"] = False
                save(
                    output / "failure.json",
                    dict(error=str(exc), completed_checks=checks),
                )
            artifacts = tuple(
                Artifact(path.relative_to(output).as_posix(), path.read_bytes())
                for path in sorted(output.rglob("*"))
                if path.is_file()
            )
        return StudyEvaluation(tuple(checks.items()), artifacts)

    def _run(
        self,
        dataset: CapturedDataset,
        root: Path,
        output: Path,
        checks: dict[str, bool],
    ) -> None:
        spec, day, intraday, plant = load_study(dataset)
        catalog = ScenarioCatalog.model_validate_json(
            captured_content(dataset, "scenario_catalog"), strict=True
        )
        if catalog.seed != spec.seed:
            raise ValueError("scenario seed differs from recipe")
        fault_start, fault_end, rearm = (
            catalog.fault_start_minute,
            catalog.fault_end_minute,
            catalog.rearm_minute,
        )
        save(
            output / "simulation_parameters.json",
            dict(
                scenario_catalog=json.loads(catalog.model_dump_json()),
                input_generator="stdlib random.Random",
                input_seed=spec.seed,
                planning_generator="numpy default_rng",
                planning_seed=20260906,
                load_drop_factor=0.25,
                voltage_fault_pu=0.85,
                telemetry_stale_seconds=300,
                nonconvergence_max_iterations=1,
                optimization_backend="SCIP MISOCP",
                network_backend="AC backward-forward sweep",
                pv_reactive_enabled=False,
                storage_reactive_enabled=False,
                wind_q_abs_over_p_max=0.328,
                artifact_scope="synthetic offline study",
            ),
        )
        for artifact in dataset_artifacts(dataset):
            path = root / artifact.path
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(artifact.content)
        selected = sorted({0, spec.intervals // 2, spec.intervals - 1})
        request_path = root / "request.json"
        fixed: list[dict[str, object]] = []
        for i in selected:
            request_path.write_bytes(
                assemble_request(dataset, spec.start + timedelta(minutes=i * 15), "normal")
            )
            request = read_snapshot_request(request_path)
            report = calculate_field_power_flow(request, root, demo=True)
            fixed.append(report)
            save(output / "fixed" / f"{i:03d}.json", report)
        checks["fixed_state_ac_converged"] = all(
            r["status"] in ("secure", "violation") for r in fixed
        )
        # The midpoint snapshot and all subsequent cases use the same captured network.
        request_path.write_bytes(
            assemble_request(
                dataset,
                spec.start + timedelta(minutes=selected[len(selected) // 2] * 15),
                "normal",
            )
        )
        request = read_snapshot_request(request_path)
        declared = {r.resource_id: r for r in spec.resources}
        if len(request.devices) != len(declared) or any(
            d.resource_id not in declared
            or d.bus_id != declared[d.resource_id].bus_id
            or d.resource_type.value != declared[d.resource_id].kind
            or d.p_max_mw != declared[d.resource_id].p_max_mw
            or d.s_max_mva != declared[d.resource_id].s_max_mva
            or d.q_max_mvar != declared[d.resource_id].q_max_mvar
            or d.q_min_mvar != -declared[d.resource_id].q_max_mvar
            or d.p_min_mw
            != (-declared[d.resource_id].p_max_mw if d.resource_type.value == "storage" else 0.0)
            for d in request.devices
        ):
            raise ValueError("recipe and field device mapping/capabilities differ")
        if {load.bus_id for load in request.loads} != {load.bus_id for load in spec.loads}:
            raise ValueError("recipe and field load mapping differ")
        if (
            request.limits.voltage_min_pu,
            request.limits.voltage_max_pu,
            request.limits.pcc_import_max_mw,
            request.limits.power_factor_min,
        ) != (spec.voltage_min_pu, spec.voltage_max_pu, spec.pcc_max_mw, spec.pf_min):
            raise ValueError("recipe and field security settings differ")
        snapshot = build_field_snapshot(request, root, demo=True)
        checks["six_independent_device_ids"] = {d.resource_id for d in snapshot.devices} == {
            r.resource_id for r in spec.resources
        }
        for r in spec.resources:
            quantity = "q_mvar" if r.kind == "svg" else "p_mw"
            perturbation = DevicePerturbation(
                r.resource_id, quantity, 0.2 if r.kind == "svg" else 0.5
            )
            comparison = compare_fixed_states(
                ComparisonRequest(
                    "fixed-device-comparison-v1",
                    request.network,
                    "normal",
                    snapshot,
                    perturbation,
                    request.limits,
                )
            )
            save(output / "comparisons" / (r.bus_id + "-" + r.kind + ".json"), comparison)
            checks["fixed_perturbation_" + r.resource_id] = (
                comparison["differences"] is not None and not comparison["redispatch_performed"]
            )
        case = make_case(spec, request.network, day)
        security = build_planning_security_trajectories(case, ["SC"])
        ahead = solve_case_ac_consistent(
            case,
            ["SC"],
            cluster_coordination=False,
            time_limit_seconds=spec.solver_seconds,
            p_grid_security_floors_mw={"SC": security["SC"].effective_floor_mw},
        )
        save(output / "day_ahead.json", ahead)
        save(output / "day_ahead_security.json", security)
        checks["day_ahead_optimization_ac_consistent"] = ahead.passed
        if not ahead.passed:
            raise RuntimeError("day-ahead optimization failed AC consistency")
        rolling = []
        day_result = cast(dict[str, Any], ahead.optimization.microgrids["SC"])
        energy = spec.storage.initial_mwh
        for i in range(spec.intervals):
            stop = min(spec.intervals, i + spec.rolling_horizon_intervals)
            current = make_case(
                spec,
                request.network,
                intraday,
                start=i,
                stop=stop,
                initial_energy=energy,
            )
            margins = build_planning_security_trajectories(current, ["SC"])
            checked = solve_case_ac_consistent(
                current,
                ["SC"],
                cluster_coordination=False,
                pcc_targets={
                    "SC": {
                        "p_mw": day_result["p_grid_mw"][i:stop],
                        "q_mvar": day_result["q_grid_mvar"][i:stop],
                    }
                },
                p_grid_security_floors_mw={"SC": margins["SC"].effective_floor_mw},
                time_limit_seconds=spec.solver_seconds,
            )
            save(
                output / "intraday" / f"{i:03d}.json",
                dict(
                    start_interval=i,
                    end_interval=stop,
                    initial_energy_mwh=energy,
                    result=checked,
                    security=margins,
                ),
            )
            if not checked.passed:
                raise RuntimeError(f"intraday interval {i} failed AC consistency")
            rolling.append(checked.optimization)
            energy = float(
                cast(dict[str, Any], checked.optimization.microgrids["SC"])["storage_energy_mwh"][1]
            )
        checks["intraday_all_intervals_ac_consistent"] = len(rolling) == spec.intervals
        plan = join_first_steps(rolling, start=spec.start)
        if {r.resource_id for r in plan.resource_schedules} != set(declared):
            raise RuntimeError("adopted schedule resource identities differ from recipe")
        checks["adopted_resource_ids_consistent"] = True
        save(output / "executed_interval_plan.json", plan)
        current = make_case(spec, request.network, intraday)
        tracking = simulate_device_tracking(
            current, current.microgrids[0], plan, plant_inputs=plant, seed=spec.seed
        )
        write_minute_network_outputs(output / "minute_normal", {"SC": tracking})
        save(
            output / "minute_normal" / "tracking.json",
            dataclasses.replace(tracking, network_feedback=[]),
        )
        actual_energy = tracking.storage_energy_mwh
        actual_power = tracking.storage_actual_mw
        svg = next(r for r in spec.resources if r.kind == "svg")
        checks["normal_svg_declared_limits"] = bool(
            np.all(np.abs(tracking.svg_reactive_actual_mvar) <= svg.q_max_mvar + 1e-7)
            and np.all(np.abs(tracking.svg_reactive_actual_mvar) <= svg.s_max_mva + 1e-7)
        )
        prior_energy = np.r_[spec.storage.initial_mwh, actual_energy[:-1]]
        energy_expected = (
            prior_energy
            - np.maximum(actual_power, 0.0) / spec.storage.eta_discharge / 60
            + np.maximum(-actual_power, 0.0) * spec.storage.eta_charge / 60
        )
        save(
            output / "storage_execution_audit.json",
            dict(
                energy_limit_interventions=int(
                    np.count_nonzero(~tracking.storage_soc_within_limits)
                ),
                legacy_flag_semantics="False means a requested response hit an energy limit; inspect actual energy separately",
                minimum_actual_mwh=float(np.min(actual_energy)),
                maximum_actual_mwh=float(np.max(actual_energy)),
                maximum_balance_error_mwh=float(np.max(abs(energy_expected - actual_energy))),
            ),
        )
        checks.update(
            minute_horizon_complete=len(tracking.time_minutes) == spec.intervals * 15,
            normal_network_valid=tracking.network_invalid_steps == 0,
            normal_network_within_limits=tracking.network_security_passed,
            normal_no_reverse_after_safety=tracking.no_reverse_violations_after_safety == 0,
            normal_storage_within_limits=bool(
                np.all(
                    (actual_energy >= spec.storage.minimum_mwh - 1e-9)
                    & (actual_energy <= spec.storage.energy_mwh + 1e-9)
                )
            ),
            normal_storage_energy_balance=bool(
                np.allclose(actual_energy, energy_expected, atol=1e-9, rtol=0.0)
            ),
            normal_power_factor=tracking.pf_violations_after_safety == 0,
        )
        start = min(spec.intervals // 2, spec.intervals - 4)
        stop = start + 4
        at = spec.start + timedelta(minutes=start * 15)
        window = plant.window(start * 15, stop * 15, at)
        fault_case = make_case(
            spec,
            request.network,
            intraday,
            start=start,
            stop=stop,
            initial_energy=float(tracking.storage_energy_mwh[start * 15 - 1])
            if start
            else spec.storage.initial_mwh,
        )
        fault_plan = slice_schedule(plan, start, stop)
        for name in catalog.cases:
            if name == "normal":
                continue
            inputs = window
            if name in ("load_drop", "wind_trip"):

                def change(series: tuple[BusSeries, ...], factor: float) -> tuple[BusSeries, ...]:
                    return tuple(
                        BusSeries(
                            bus_id=s.bus_id,
                            values=tuple(
                                v * factor if fault_start <= j < fault_end else v
                                for j, v in enumerate(s.values)
                            ),
                        )
                        for s in series
                    )

                values = dict(
                    start=window.start,
                    step_minutes=window.step_minutes,
                    load_p=window.load_p,
                    load_q=window.load_q,
                    wind_available=window.wind_available,
                    pv_available=window.pv_available,
                )
                if name == "load_drop":
                    values.update(
                        load_p=change(window.load_p, 0.25),
                        load_q=change(window.load_q, 0.25),
                    )
                else:
                    values.update(wind_available=change(window.wind_available, 0.0))
                inputs = PlantInputs.model_validate(values)

            def fault(snapshot: FixedStateSnapshot, stage: str) -> FixedStateSnapshot:
                minute = int((snapshot.at - at).total_seconds() / 60)
                if fault_start <= minute < fault_end:
                    if name == "bad_quality":
                        return dataclasses.replace(snapshot, quality_valid=False)
                    if name == "stale_telemetry":
                        return dataclasses.replace(snapshot, at=snapshot.at - timedelta(minutes=5))
                    if name == "voltage_violation":
                        return dataclasses.replace(snapshot, slack_voltage_pu=0.85)
                return snapshot

            limits = (
                dataclasses.replace(request.limits, max_iterations=1)
                if name == "nonconvergence"
                else request.limits
            )
            result = simulate_device_tracking(
                fault_case,
                fault_case.microgrids[0],
                fault_plan,
                plant_inputs=inputs,
                network_snapshot_adapter=fault,
                network_limits=limits,
                network_rearm_steps=(rearm,),
                seed=spec.seed,
            )
            write_minute_network_outputs(output / "faults" / name, {"SC": result})
            save(
                output / "faults" / name / "tracking.json",
                dataclasses.replace(result, network_feedback=[]),
            )
            save(
                output / "faults" / name / "plant_inputs.json",
                json.loads(inputs.model_dump_json()),
            )
            if name in ("bad_quality", "stale_telemetry", "voltage_violation"):
                checks[name + "_blocked"] = bool(
                    np.all(result.network_recovery_blocked[fault_start:rearm])
                )
                checks[name + "_explicit_rearm"] = not bool(result.network_recovery_blocked[rearm])
                checks[name + "_no_restore_while_blocked"] = not bool(
                    np.any(result.group_requested_restoration_mw[fault_start:rearm] > 0.0)
                )
            elif name == "nonconvergence":
                checks[name + "_unknown_and_blocked"] = result.network_invalid_steps == 60 and bool(
                    np.all(result.network_recovery_blocked)
                )
            elif name == "load_drop":
                checks[name + "_safety_triggered"] = (
                    result.no_reverse_violations_before_safety > 0
                    and result.safety_interventions > 0
                )
                checks[name + "_no_reverse_after_safety"] = (
                    result.no_reverse_violations_after_safety == 0
                )
            elif name == "wind_trip":
                checks[name + "_availability_zero"] = bool(
                    np.all(result.wind_available_mw[fault_start:fault_end] == 0.0)
                )
                checks[name + "_actual_zero"] = bool(
                    np.all(result.wind_actual_mw[fault_start:fault_end] == 0.0)
                )
                checks[name + "_network_valid"] = result.network_invalid_steps == 0
        group = run_group_control_scenarios()
        save(output / "group_control_scenarios.json", group)
        checks.update({"group_" + name: value.passed for name, value in group.items()})
        checks["execution_completed"] = True
