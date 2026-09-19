"""1分钟风光储设备响应、光伏群控和本地安全保护闭环仿真。"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timedelta, timezone
from math import acos, exp, tan
from typing import Callable

import numpy as np

from .actuation_arbiter import ActuationArbiter
from .control_contracts import (
    CapIntentAction,
    GroupTelemetrySnapshot,
    HardProtectionStatus,
    PccSafetyConstraint,
    PVStationSnapshot,
    ReverseFlowRiskEstimate,
    SignalValidity,
    StationActuationFeedback,
    StationCapIntent,
)
from .data import MicrogridData, ProjectCase
from .execution_coordinator import ShanchengSafetyCoordinator
from .group_control import GroupControlSupervisor, allocate_pv_curtailment
from .hard_protection import HardCapReleaseGuard
from .hierarchy_types import (
    DeviceTrackingResult,
    GroupControlAction,
    GroupControlConfig,
    GroupControlState,
    TimeScaleConfig,
)
from .minute_network import (
    MinuteNetworkConfig,
    MinuteNetworkEvaluator,
    MinuteNetworkFeedback,
    NetworkRecoveryInterlock,
)
from .model import OptimizationResult
from .modules.dispatch.contracts import AdoptedSchedule
from .modules.control.api import (
    CurtailmentLedger,
    RestorationMonitor,
    constrain_storage_power,
    disaggregate_executed_generation,
    execution_substeps,
    limit_synthetic_generation,
)
from .modules.control.contracts import (
    PlantInputs,
    RestorationCommand,
    RestorationObservation,
    RestorationStation,
    StorageDynamicsRecord,
)
from .network_model import assess_network_model
from .network_scenarios import NetworkSecurityLimits
from .power_flow_comparison import BusLoadState, DeviceState, FixedStateSnapshot
from .reactive_correction import correct_reactive_dispatch, reactive_response
from .reactive_execution import (
    ReactiveCapability,
    allocate_bounded_reactive_power,
    calculate_reactive_capabilities,
)
from .resource_control_contracts import (
    ResourceSchedule,
    ResourceType,
    validate_resource_schedules,
)
from .shancheng_control import (
    ChannelActuationFeedback,
    CommandDisposition,
    LegacyCommandFactory,
    ShanchengControlConfig,
    ShanchengControlDecision,
    ShanchengController,
    ShanchengControlMode,
    ShanchengTelemetry,
    StorageInterlocks,
    StorageTelemetry,
    SvgTelemetry,
    WindTurbineTelemetry,
)
from .shancheng_control import (
    SignalValidity as ShanchengSignalValidity,
)


def _approach(current: float, target: float, alpha: float, ramp: float) -> float:
    desired = current + alpha * (target - current)
    return current + float(np.clip(desired - current, -ramp, ramp))


def _optimization_resource_schedules(
    data: dict[str, object],
    *,
    expected_time_steps: int,
) -> tuple[ResourceSchedule, ...]:
    """Require the identity-preserving optimization output contract."""

    raw = data.get("resource_schedules")
    if not isinstance(raw, tuple) or not all(
        isinstance(item, ResourceSchedule) for item in raw
    ):
        raise ValueError(
            "device tracking requires typed per-resource optimization schedules"
        )
    return validate_resource_schedules(raw, expected_time_steps=expected_time_steps)


def _allocate_nonnegative_total(
    total: float,
    reference: np.ndarray,
    upper: np.ndarray,
    resource_ids: tuple[str, ...],
) -> np.ndarray:
    """Adapt arrays to the public ID-based conservation contract."""
    if reference.shape != upper.shape or reference.shape != (len(resource_ids),):
        raise ValueError("generation vectors must align with resource IDs")
    distributed = disaggregate_executed_generation(
        total,
        {rid: max(0.0, float(reference[i])) for i, rid in enumerate(resource_ids)},
        {rid: float(upper[i]) for i, rid in enumerate(resource_ids)},
    )
    return np.asarray([item.actual_mw for item in distributed])


def _approach_reactive_vector(
    current: np.ndarray,
    target: np.ndarray,
    *,
    alpha: float,
    aggregate_ramp_mvar: float,
) -> np.ndarray:
    """Apply one aggregate Q-ramp budget across explicit device responses."""

    return reactive_response(current, target, alpha=alpha, ramp_mvar=aggregate_ramp_mvar)


def _reactive_targets_from_shancheng(
    decision: ShanchengControlDecision,
) -> tuple[float, float] | None:
    """Extract wind/SVG Q writes only; other channel fields remain unowned."""

    if decision.reactive_mode is not ShanchengControlMode.DISPATCH_TRACKING:
        return None
    wind = tuple(item.reactive_power_mvar for item in decision.setpoints.wind)
    svg = decision.setpoints.svg_capacitive_reactive_power_mvar
    if svg is None or any(value is None for value in wind):
        return None
    return float(sum(wind)), float(svg)


def _release_group_caps(
    arbiter: ActuationArbiter,
    station_ids: tuple[str, ...],
    baseline_mw: np.ndarray,
    request_mw: float,
    *,
    decision_id: str,
) -> tuple[StationCapIntent, ...]:
    """只放松群控自己拥有的绝对 cap，并显式释放已完全恢复的约束。"""

    owner_caps = arbiter.owner_caps("group_control")
    owned = np.asarray([
        max(0.0, float(baseline_mw[index]) - owner_caps.get(station_id, float("inf")))
        for index, station_id in enumerate(station_ids)
    ])
    total = float(np.sum(owned))
    released = min(max(0.0, request_mw), total)
    if released <= 0.0 or total <= 0.0:
        return ()

    intents = []
    shares = released * owned / total
    for index, station_id in enumerate(station_ids):
        if owned[index] <= 1e-12:
            continue
        current_cap = owner_caps[station_id]
        new_cap = current_cap + float(shares[index])
        # Keep ownership through the last response evaluation. A released cap
        # is not proof of executed restoration; successful completion releases it.
        intents.append(
            StationCapIntent(
                owner_id="group_control",
                station_id=station_id,
                action=CapIntentAction.SET_CAP,
                absolute_cap_mw=min(new_cap, float(baseline_mw[index])),
                decision_id=decision_id,
            )
        )
    return tuple(intents)


def _proportional_reduce(actual_mw: np.ndarray, request_mw: float) -> float:
    """按当前出力比例执行本地硬保护的瞬时削减。"""
    total = float(np.sum(actual_mw))
    reduction = min(max(0.0, request_mw), total)
    if reduction <= 0.0 or total <= 0.0:
        return 0.0
    actual_mw -= reduction * actual_mw / total
    np.maximum(actual_mw, 0.0, out=actual_mw)
    return reduction


def _storage_response(
    requested_power_mw: float,
    energy_before_mwh: float,
    microgrid: MicrogridData,
    dt_hours: float,
) -> tuple[float, float, bool]:
    """Apply one synthetic PCS response and integrate energy exactly once."""

    storage = microgrid.storage
    actual = float(np.clip(
        requested_power_mw,
        -storage.p_max_mw,
        storage.p_max_mw,
    ))
    before_energy_limit = actual
    if actual >= 0.0:
        max_discharge = max(
            0.0,
            (energy_before_mwh - storage.e_min_mwh)
            * storage.eta_discharge
            / dt_hours,
        )
        actual = min(actual, max_discharge)
        energy_after = (
            energy_before_mwh
            - actual * dt_hours / storage.eta_discharge
        )
    else:
        max_charge = max(
            0.0,
            (storage.e_max_mwh - energy_before_mwh)
            / (storage.eta_charge * dt_hours),
        )
        actual = max(actual, -max_charge)
        energy_after = (
            energy_before_mwh
            + (-actual) * storage.eta_charge * dt_hours
        )
    return (
        actual,
        float(np.clip(energy_after, storage.e_min_mwh, storage.e_max_mwh)),
        abs(actual - before_energy_limit) <= 1e-9,
    )


def _synthetic_shancheng_snapshot(
    *,
    microgrid: MicrogridData,
    snapshot_index: int,
    time_minutes: float,
    step_minutes: float,
    wind_actual_mw: float,
    wind_available_mw: float,
    wind_reactive_actual_mvar: float,
    storage_actual_mw: float,
    storage_energy_mwh: float,
    svg_reactive_actual_mvar: float,
    active_feedback: ChannelActuationFeedback,
    reactive_feedback: ChannelActuationFeedback,
) -> ShanchengTelemetry:
    """Adapt aggregate simulation state to the device controller contract.

    The parameterized network has one aggregate wind injection per microgrid.
    Real ShanCheng integration must replace this synthetic adapter with the two
    turbine D5000 snapshot. The SIL adapter retains an aggregate wind device,
    but its P/Q values now come from explicit resource execution state.
    """

    rated_wind = max(
        float(sum(microgrid.wind_capacity_mw.values())),
        wind_actual_mw,
        wind_available_mw,
        1e-6,
    )
    valid = ShanchengSignalValidity(True, True)
    return ShanchengTelemetry(
        snapshot_id=f"{microgrid.name}:device:{snapshot_index}",
        observed_at_utc=(
            datetime(2026, 1, 1, tzinfo=timezone.utc)
            + timedelta(minutes=time_minutes)
        ),
        elapsed_minutes=time_minutes,
        clock_hour=(time_minutes / 60.0) % 24.0,
        step_minutes=step_minutes,
        wind_turbines=(WindTurbineTelemetry(
            name=f"{microgrid.name}:WIND_AGGREGATE",
            active_power_mw=max(0.0, wind_actual_mw),
            reactive_power_mvar=wind_reactive_actual_mvar,
            available_active_power_mw=max(0.0, wind_available_mw),
            rated_active_power_mw=rated_wind,
            active_power_validity=valid,
            reactive_power_validity=valid,
            available_power_validity=valid,
            status_validity=valid,
        ),),
        storage=StorageTelemetry(
            active_power_mw=storage_actual_mw,
            energy_mwh=storage_energy_mwh,
            energy_min_mwh=microgrid.storage.e_min_mwh,
            energy_max_mwh=microgrid.storage.e_max_mwh,
            charge_power_max_mw=microgrid.storage.p_max_mw,
            discharge_power_max_mw=microgrid.storage.p_max_mw,
            charge_efficiency=microgrid.storage.eta_charge,
            discharge_efficiency=microgrid.storage.eta_discharge,
            interlocks=StorageInterlocks(),
            active_power_validity=valid,
            energy_validity=valid,
            status_validity=valid,
            interlocks_validity=valid,
        ),
        svg=SvgTelemetry(
            reactive_power_mvar=svg_reactive_actual_mvar,
            reactive_power_validity=valid,
            status_validity=valid,
        ),
        active_actuation=active_feedback,
        reactive_actuation=reactive_feedback,
    )


def _active_targets_from_shancheng(
    decision: ShanchengControlDecision | None,
) -> tuple[float, float] | None:
    """Extract the sole wind/storage write proposal from an accepted plan."""

    if decision is None:
        return None
    if decision.active_disposition not in (
        CommandDisposition.ACCEPTED,
        CommandDisposition.TRACKING,
    ):
        return None
    wind_values = tuple(
        item.active_power_mw for item in decision.setpoints.wind
    )
    if any(value is None for value in wind_values):
        return None
    storage = decision.setpoints.storage_active_power_mw
    if storage is None:
        return None
    return float(sum(wind_values)), float(storage)


@dataclass(frozen=True)
class _SyntheticShanchengActuation:
    """Result returned by the explicit SIL device adapter."""

    wind_actual_mw: float
    storage_actual_mw: float
    storage_energy_mwh: float
    storage_soc_within_limits: bool
    feedback: ChannelActuationFeedback


def _apply_synthetic_shancheng_active_decision(
    decision: ShanchengControlDecision | None,
    *,
    energy_before_mwh: float,
    microgrid: MicrogridData,
    dt_hours: float,
) -> _SyntheticShanchengActuation | None:
    """Execute one accepted controller proposal in the SIL plant adapter.

    This is the sole place where a new hard-protection wind/storage proposal is
    converted to simulated plant values. Production code must replace it with
    southbound writes plus real acknowledgement/echo telemetry.
    """

    targets = _active_targets_from_shancheng(decision)
    if targets is None or decision is None or decision.command_id is None:
        return None
    requested_wind, requested_storage = targets
    storage_actual, energy_after, storage_within_limits = _storage_response(
        requested_storage,
        energy_before_mwh,
        microgrid,
        dt_hours,
    )
    return _SyntheticShanchengActuation(
        wind_actual_mw=requested_wind,
        storage_actual_mw=storage_actual,
        storage_energy_mwh=energy_after,
        storage_soc_within_limits=storage_within_limits,
        feedback=ChannelActuationFeedback(
            execution_known=True,
            last_request_id=decision.command_id,
            last_acknowledged_request_id=decision.command_id,
        ),
    )


def simulate_device_tracking(
    case: ProjectCase,
    microgrid: MicrogridData,
    regional_result: OptimizationResult | AdoptedSchedule,
    *,
    config: TimeScaleConfig | None = None,
    group_control_config: GroupControlConfig | None = None,
    reverse_flow_probability_15: np.ndarray | None = None,
    network_config: MinuteNetworkConfig | None = None,
    network_limits: NetworkSecurityLimits | None = None,
    network_snapshot_adapter: Callable[[FixedStateSnapshot, str], FixedStateSnapshot] | None = None,
    network_rearm_steps: tuple[int, ...] = (),
    slack_voltage_pu: float = 1.0,
    seed: int = 20260901,
    plant_inputs: PlantInputs | None = None,
) -> DeviceTrackingResult:
    """仿真独立风光储响应，并在本地硬保护之前运行光伏群控监督器。"""
    cfg = config or TimeScaleConfig()
    group_cfg = group_control_config or GroupControlConfig()
    substeps = execution_substeps(case.assumptions.dt_hours * 60, cfg.device_step_minutes)
    minutes_per_interval = substeps * cfg.device_step_minutes
    if isinstance(regional_result, AdoptedSchedule):
        if (
            regional_result.microgrid_id != microgrid.name
            or regional_result.step_minutes != minutes_per_interval
        ):
            raise ValueError("adopted schedule microgrid/time step differs from execution case")
        if plant_inputs is not None and regional_result.start != plant_inputs.start:
            raise ValueError("adopted schedule and plant must start at the same instant")
        data = {
            "p_grid_mw": np.asarray(regional_result.p_grid_mw),
            "q_grid_mvar": np.asarray(regional_result.q_grid_mvar),
            "resource_schedules": tuple(
                ResourceSchedule(
                    r.resource_id,
                    r.bus_id,
                    ResourceType(r.resource_type),
                    np.asarray(r.active_power_mw),
                    np.asarray(r.reactive_power_mvar),
                )
                for r in regional_result.resource_schedules
            ),
        }
    else:
        data = regional_result.microgrids[microgrid.name]
    if group_cfg.decision_interval_minutes != cfg.device_step_minutes:
        raise ValueError(
            "group-control decision interval must equal the device simulation step"
        )
    if group_cfg.feature_interval_minutes % cfg.device_step_minutes != 0:
        raise ValueError("device step must divide the group-control feature interval")
    T = len(case.time_hours)
    total_steps = T * substeps
    rng = np.random.default_rng(seed)
    if cfg.device_time_constant_minutes <= 0.0:
        raise ValueError("device_time_constant_minutes must be positive")
    if cfg.pv_device_time_constant_minutes <= 0.0:
        raise ValueError("pv_device_time_constant_minutes must be positive")
    alpha = 1.0 - exp(-cfg.device_step_minutes / cfg.device_time_constant_minutes)
    pv_alpha = 1.0 - exp(
        -cfg.device_step_minutes / cfg.pv_device_time_constant_minutes
    )
    p_ramp = cfg.active_power_ramp_mw_per_minute * cfg.device_step_minutes
    q_ramp = cfg.reactive_power_ramp_mvar_per_minute * cfg.device_step_minutes

    p_command_15 = np.asarray(data["p_grid_mw"], dtype=float)
    q_command_15 = np.asarray(data["q_grid_mvar"], dtype=float)
    resource_schedules = _optimization_resource_schedules(
        data, expected_time_steps=T
    )
    wind_schedules = tuple(
        item for item in resource_schedules
        if item.resource_type is ResourceType.WIND
    )
    pv_schedules = tuple(
        item for item in resource_schedules
        if item.resource_type is ResourceType.PV
    )
    storage_schedules = tuple(
        item for item in resource_schedules
        if item.resource_type is ResourceType.STORAGE
    )
    svg_schedules = tuple(
        item for item in resource_schedules
        if item.resource_type is ResourceType.SVG
    )
    if not wind_schedules:
        raise ValueError("device tracking requires at least one wind resource")
    if len(storage_schedules) != 1 or len(svg_schedules) != 1:
        raise ValueError("device tracking requires exactly one storage and one SVG")
    wind_command_by_resource_15 = np.vstack([
        item.active_power_mw for item in wind_schedules
    ])
    wind_command_15 = np.sum(wind_command_by_resource_15, axis=0)
    pv_command_by_bus_15 = (
        np.vstack([item.active_power_mw for item in pv_schedules])
        if pv_schedules else np.zeros((0, T))
    )
    pv_command_15 = np.sum(pv_command_by_bus_15, axis=0)
    if reverse_flow_probability_15 is not None:
        reverse_flow_probability_15 = np.asarray(
            reverse_flow_probability_15, dtype=float
        )
        if reverse_flow_probability_15.shape != (T,):
            raise ValueError("reverse_flow_probability_15 must align with optimization steps")
        if (
            not np.all(np.isfinite(reverse_flow_probability_15))
            or np.min(reverse_flow_probability_15) < 0.0
            or np.max(reverse_flow_probability_15) > 1.0
        ):
            raise ValueError("reverse_flow_probability_15 must be finite and in [0, 1]")
    pv_buses = tuple(item.bus_id for item in pv_schedules)
    if set(pv_buses) != set(microgrid.pv_available_mw):
        raise ValueError("PV resource schedules do not match microgrid buses")
    pv_plan_by_bus_15 = pv_command_by_bus_15
    pv_available_by_bus_15 = (
        np.vstack([
            np.asarray(microgrid.pv_available_mw[bus], dtype=float)
            for bus in pv_buses
        ])
        if pv_buses else np.zeros((0, T))
    )
    storage_command_15 = storage_schedules[0].active_power_mw
    reactive_resource_ids = tuple(item.resource_id for item in resource_schedules)
    reactive_plan_15 = np.vstack([
        item.reactive_power_mvar for item in resource_schedules
    ])
    schedule_index = {
        item.resource_id: index for index, item in enumerate(resource_schedules)
    }
    wind_resource_indices = np.asarray([
        schedule_index[item.resource_id] for item in wind_schedules
    ], dtype=int)
    pv_resource_indices = np.asarray([
        schedule_index[item.resource_id] for item in pv_schedules
    ], dtype=int)
    storage_resource_index = schedule_index[storage_schedules[0].resource_id]
    svg_resource_index = schedule_index[svg_schedules[0].resource_id]
    if set(item.bus_id for item in wind_schedules) != set(microgrid.wind_available_mw):
        raise ValueError("wind resource schedules do not match microgrid buses")
    wind_available_by_resource_15 = np.vstack([
        np.asarray(microgrid.wind_available_mw[item.bus_id], dtype=float)
        for item in wind_schedules
    ])
    if storage_schedules[0].bus_id != microgrid.storage.bus:
        raise ValueError("storage resource schedule has the wrong connection bus")
    if svg_schedules[0].bus_id != microgrid.svg_bus:
        raise ValueError("SVG resource schedule has the wrong connection bus")
    q_plan_by_resource = np.repeat(reactive_plan_15, substeps, axis=1)
    # Gross loads come from buses, never from PCC + scheduled generation (which
    # already contains planned network losses and fixed-shunt effects).
    required_load_buses = set(microgrid.buses) - {microgrid.pcc_bus}
    if not required_load_buses.issubset(microgrid.load_p_mw) or not required_load_buses.issubset(microgrid.load_q_mvar):
        raise ValueError("missing gross bus load profiles; no synthetic redistribution is allowed")
    gross_p_15 = np.vstack([np.asarray(microgrid.load_p_mw.get(bus, np.zeros(T))) for bus in microgrid.buses])
    gross_q_15 = np.vstack([np.asarray(microgrid.load_q_mvar.get(bus, np.zeros(T))) for bus in microgrid.buses])
    if gross_p_15.shape != (len(microgrid.buses), T) or gross_q_15.shape != gross_p_15.shape:
        raise ValueError("minute bus load profiles must align with the time horizon")
    if not np.all(np.isfinite(gross_p_15)) or not np.all(np.isfinite(gross_q_15)) or np.any(gross_p_15 < 0):
        raise ValueError("minute simulation requires finite gross loads; invalid live snapshots must be marked by the adapter")

    p_cmd = np.repeat(p_command_15, substeps)
    q_cmd = np.repeat(q_command_15, substeps)
    load_p_factor = np.clip(
        1.0 + rng.normal(0.0, 0.008, total_steps), 0.96, 1.04
    )
    load_q_factor = np.clip(
        1.0 + rng.normal(0.0, 0.008, total_steps), 0.96, 1.04
    )
    gross_p = np.repeat(gross_p_15, substeps, axis=1) * load_p_factor
    gross_q = np.repeat(gross_q_15, substeps, axis=1) * load_q_factor
    # Effective aggregate demand is refreshed from AC for legacy local controllers.
    # It is never passed back into the AC solver as a gross load.
    net_demand_p = np.sum(gross_p, axis=0)
    net_demand_q = np.sum(gross_q, axis=0)
    wind_cmd = np.repeat(wind_command_15, substeps)
    wind_command_by_resource = np.repeat(
        wind_command_by_resource_15, substeps, axis=1
    )
    wind_factor = np.clip(
        1.0 + rng.normal(0.0, 0.015, total_steps), 0.92, 1.08
    )
    wind_available_by_resource = (
        np.repeat(wind_available_by_resource_15, substeps, axis=1)
        * wind_factor[None, :]
    )
    wind_avail = np.sum(wind_available_by_resource, axis=0)
    pv_factor = np.clip(
        1.0 + rng.normal(0.0, 0.015, total_steps), 0.92, 1.08
    )
    pv_plan_by_bus = np.repeat(pv_plan_by_bus_15, substeps, axis=1)
    pv_available_by_bus = (
        np.repeat(pv_available_by_bus_15, substeps, axis=1)
        * pv_factor[None, :]
    )
    storage_cmd = np.repeat(storage_command_15, substeps)

    if plant_inputs is not None:
        if plant_inputs.steps != total_steps or plant_inputs.step_minutes != cfg.device_step_minutes:
            raise ValueError("explicit plant inputs must match the simulation horizon and step")

        def explicit_matrix(series, buses):
            lookup = {item.bus_id: item.values for item in series}
            if set(lookup) != set(buses):
                raise ValueError("explicit plant bus coverage does not match the case")
            return np.asarray([lookup[bus] for bus in buses], dtype=float)

        gross_p = explicit_matrix(plant_inputs.load_p, microgrid.buses)
        gross_q = explicit_matrix(plant_inputs.load_q, microgrid.buses)
        wind_available_by_resource = explicit_matrix(
            plant_inputs.wind_available, [item.bus_id for item in wind_schedules])
        pv_available_by_bus = explicit_matrix(plant_inputs.pv_available, pv_buses)
        for i, item in enumerate(wind_schedules):
            if np.any(wind_available_by_resource[i] > microgrid.wind_capacity_mw[item.bus_id] + 1e-9):
                raise ValueError("explicit wind availability exceeds nameplate capacity")
        for i, bus in enumerate(pv_buses):
            if np.any(pv_available_by_bus[i] > microgrid.pv_capacity_mw[bus] + 1e-9):
                raise ValueError("explicit PV availability exceeds nameplate capacity")
        wind_avail = np.sum(wind_available_by_resource, axis=0)
        net_demand_p = np.sum(gross_p, axis=0)
        net_demand_q = np.sum(gross_q, axis=0)

    # Both generated and explicit plants share one physical availability model.
    wind_resource_ids = tuple(item.resource_id for item in wind_schedules)
    wind_rated = np.asarray([microgrid.wind_capacity_mw[s.bus_id] for s in wind_schedules])
    pv_rated = np.asarray([microgrid.pv_capacity_mw[bus] for bus in pv_buses])
    wind_available_by_resource = np.asarray(
        [
            [limit_synthetic_generation(float(v), float(v), float(wind_rated[i])) for v in row]
            for i, row in enumerate(wind_available_by_resource)
        ]
    )
    pv_available_by_bus = np.asarray(
        [
            [limit_synthetic_generation(float(v), float(v), float(pv_rated[i])) for v in row]
            for i, row in enumerate(pv_available_by_bus)
        ]
    ).reshape(len(pv_buses), total_steps)
    wind_avail = np.sum(wind_available_by_resource, axis=0)
    wind_resource_actual = np.zeros((len(wind_schedules), total_steps))
    wind_availability_limited = np.zeros(total_steps)
    pv_availability_limited = np.zeros(total_steps)

    p_actual = np.zeros(total_steps)
    q_actual = np.zeros(total_steps)
    wind_actual_series = np.zeros(total_steps)
    pv_actual_series = np.zeros(total_steps)
    pv_control_target_series = np.zeros(total_steps)
    pv_actual_by_bus_series = np.zeros((len(pv_buses), total_steps))
    pv_control_target_by_bus_series = np.zeros((len(pv_buses), total_steps))
    storage_actual_series = np.zeros(total_steps)
    storage_dynamics = []
    reactive_target_by_resource_series = np.zeros(
        (len(resource_schedules), total_steps)
    )
    reactive_actual_by_resource_series = np.zeros(
        (len(resource_schedules), total_steps)
    )
    reactive_dispatch_unserved = np.zeros(total_steps)
    shancheng_reactive_command_accepted = np.zeros(total_steps, dtype=bool)
    reactive_execution_known = np.zeros(total_steps, dtype=bool)
    hard_wind_storage_target = np.full(total_steps, np.nan)
    hard_safety_unserved = np.zeros(total_steps)
    hard_wind_storage_command_accepted = np.zeros(total_steps, dtype=bool)
    group_risk_limit = np.zeros(total_steps)
    group_safety_threshold = np.zeros(total_steps)
    group_restore_threshold = np.zeros(total_steps)
    group_current_net_load = np.zeros(total_steps)
    group_load_change_rate = np.zeros(total_steps)
    group_pv_penetration = np.zeros(total_steps)
    group_reverse_flow_probability = np.zeros(total_steps)
    group_observed_reverse_flow_probability = np.full(total_steps, np.nan)
    group_reverse_flow_probability_valid = np.zeros(total_steps, dtype=bool)
    group_risk_index = np.zeros(total_steps)
    group_required_curtailment = np.zeros(total_steps)
    group_requested_curtailment = np.zeros(total_steps)
    group_unserved_curtailment = np.zeros(total_steps)
    group_requested_restoration = np.zeros(total_steps)
    group_achieved_restoration = np.full(total_steps, np.nan)
    restoration_evidence = []
    group_remaining_curtailment = np.zeros(total_steps)
    group_recovery_dwell_remaining = np.zeros(total_steps)
    group_recovery_evaluation_passed = np.full(total_steps, np.nan)
    group_restoration_response_error = np.zeros(total_steps)
    measured_pv_curtailment = np.zeros(total_steps)
    pcc_before_local_safety = np.zeros(total_steps)
    local_reverse_reduction = np.zeros(total_steps)
    group_hard_override_active = np.zeros(total_steps, dtype=bool)
    local_pf_adjustment = np.zeros(total_steps)
    actual_power_factor = np.ones(total_steps)
    storage_energy_series = np.zeros(total_steps)
    storage_soc_compliant_series = np.ones(total_steps, dtype=bool)
    group_states = np.empty(total_steps, dtype=object)
    group_actions = np.empty(total_steps, dtype=object)
    group_reasons = np.empty(total_steps, dtype=object)

    wind_actual = min(float(wind_cmd[0]), float(wind_avail[0]))
    initial_pv_target = np.minimum(
        pv_plan_by_bus[:, 0],
        pv_available_by_bus[:, 0],
    )
    pv_uncontrolled_actual = initial_pv_target.copy()
    pv_actual_by_bus = initial_pv_target.copy()
    station_ids = tuple(f"{microgrid.name}:{bus}" for bus in pv_buses)
    arbiter = ActuationArbiter()
    configured_wind_ratios = tuple(
        microgrid.wind_q_over_p_limit(item.bus_id)
        for item in wind_schedules
        if microgrid.wind_q_over_p_limit(item.bus_id) is not None
    )
    wind_reactive_ratio = (
        min(float(value) for value in configured_wind_ratios)
        if configured_wind_ratios else 0.30
    )
    svg_dispatch_limit = min(
        abs(microgrid.svg_capability().effective_q_min_mvar),
        abs(microgrid.svg_capability().effective_q_max_mvar),
    )
    shancheng_controller = ShanchengController(ShanchengControlConfig(
        wind_reactive_ratio=wind_reactive_ratio,
        svg_dispatch_limit_mvar=svg_dispatch_limit,
        local_svg_capacitive_mvar=min(1.5, svg_dispatch_limit),
    ))
    safety_coordinator = ShanchengSafetyCoordinator(
        controller=shancheng_controller,
        source_id=f"{microgrid.name}:hard-protection",
        source_epoch=f"{microgrid.name}:synthetic-session",
    )
    reactive_command_factory = LegacyCommandFactory(
        source_id=f"{microgrid.name}:reactive-plan",
        source_epoch=f"{microgrid.name}:synthetic-reactive-session",
        validity_seconds=60.0,
    )
    reactive_command_factory.authorize(shancheng_controller)
    active_actuation_feedback = ChannelActuationFeedback(execution_known=True)
    reactive_actuation_feedback = ChannelActuationFeedback(execution_known=True)
    hard_protection_latched = False
    hard_release_guard = HardCapReleaseGuard(
        hysteresis_mw=cfg.hard_release_hysteresis_mw,
        confirmation_cycles=cfg.hard_release_confirmation_cycles,
    )
    pv_installed = np.asarray(
        [microgrid.pv_capacity_mw[bus] for bus in pv_buses],
        dtype=float,
    )
    installed_pv_total = float(np.sum(pv_installed))
    pv_ramp_per_minute = (
        cfg.active_power_ramp_mw_per_minute
        * pv_installed
        / installed_pv_total
        if installed_pv_total > 0.0
        else np.zeros(0)
    )
    pv_ramp_per_step = pv_ramp_per_minute * cfg.device_step_minutes
    storage_actual = float(storage_cmd[0])
    reactive_actual_by_resource = q_plan_by_resource[:, 0].copy()
    energy = microgrid.storage.e_initial_mwh
    dt_hours = cfg.device_step_minutes / 60.0
    pf_tan = tan(acos(case.assumptions.pf_min))
    before_violations = 0
    after_violations = 0
    pf_violations = 0
    local_reverse_interventions = 0
    local_pf_interventions = 0
    group_curtailment_commands = 0
    group_restoration_commands = 0
    group_recovery_aborts = 0
    supervisor = GroupControlSupervisor(group_cfg)
    active_safety_floor_mw = 0.0
    restoration_monitor = RestorationMonitor(
        dwell_minutes=group_cfg.recovery_pause_minutes,
        interval_minutes=cfg.device_step_minutes,
    )
    curtailment_ledger = CurtailmentLedger()
    restoration_command_id: str | None = None
    feature_steps = group_cfg.feature_interval_minutes // cfg.device_step_minutes
    current_net_load_feature = 0.0
    previous_net_load_feature = 0.0

    minute_network_config = network_config or MinuteNetworkConfig()
    evaluator = MinuteNetworkEvaluator(microgrid.network_model_v2,
        network_limits or NetworkSecurityLimits(microgrid.voltage_min_pu, microgrid.voltage_max_pu,
                                                microgrid.p_grid_min_mw, microgrid.p_grid_max_mw,
                                                case.assumptions.pf_min),
        {item.resource_id: item.bus_id for item in resource_schedules}, config=minute_network_config)
    network_gate = NetworkRecoveryInterlock(minute_network_config.safe_confirmation_cycles, cfg.device_step_minutes * 60)
    network_records: list[dict] = []
    network_blocked = np.zeros(total_steps, dtype=bool)
    network_invalid_steps = 0
    network_violation_steps = 0
    simulation_epoch = plant_inputs.start if plant_inputs is not None else datetime(2026, 9, 1, tzinfo=timezone.utc)
    mode_id = microgrid.network_operating_mode_id or (
        microgrid.network_model_v2.default_operating_mode_id if microgrid.network_model_v2 else "missing")

    def observe_network(stage: str, *, pv=None, wind=None, storage=None, refresh=True):
        now = simulation_epoch + timedelta(minutes=time_minutes)
        wind_value = wind_actual if wind is None else wind
        pv_values = pv_actual_by_bus if pv is None else pv
        storage_value = storage_actual if storage is None else storage
        # This disaggregation is explicitly part of the synthetic plant. Field
        # callers supply independent measured device values to MinuteNetworkEvaluator.
        wind_values = _allocate_nonnegative_total(
            wind_value,
            wind_command_by_resource[:, k],
            wind_available_by_resource[:, k],
            wind_resource_ids,
        )
        active = {item.resource_id: 0.0 for item in resource_schedules}
        active.update({item.resource_id: float(wind_values[i]) for i, item in enumerate(wind_schedules)})
        active.update({item.resource_id: float(pv_values[i]) for i, item in enumerate(pv_schedules)})
        active[storage_schedules[0].resource_id] = float(storage_value)
        try:
            snapshot = FixedStateSnapshot(
                f"{microgrid.name}:{k}:{stage}",
                now,
                "synthetic minute plant: gross bus loads and executed device P/Q; wind disaggregation is simulated",
                "gross_bus_load",
                slack_voltage_pu,
                True,
                True,
                tuple(
                    BusLoadState(bus, float(gross_p[i, k]), float(gross_q[i, k]))
                    for i, bus in enumerate(microgrid.buses)
                ),
                tuple(
                    DeviceState(
                        item.resource_id,
                        item.bus_id,
                        item.resource_type,
                        active[item.resource_id],
                        float(reactive_actual_by_resource[i]),
                        True,
                        q_min_mvar=microgrid.svg_capability().q_min_mvar
                        if item.resource_type is ResourceType.SVG
                        else None,
                        q_max_mvar=microgrid.svg_capability().q_max_mvar
                        if item.resource_type is ResourceType.SVG
                        else None,
                        s_max_mva=microgrid.svg_capability().s_max_mva
                        if item.resource_type is ResourceType.SVG
                        else None,
                    )
                    for i, item in enumerate(resource_schedules)
                ),
            )
            if network_snapshot_adapter is not None:
                snapshot = network_snapshot_adapter(snapshot, stage)
            if not isinstance(snapshot, FixedStateSnapshot):
                raise TypeError("network snapshot adapter must return FixedStateSnapshot")
            feedback = evaluator.evaluate(snapshot, at=now, operating_mode_id=mode_id)
        except (ValueError, TypeError, OverflowError, FloatingPointError) as exc:
            feedback = MinuteNetworkFeedback(now, None, f"{microgrid.name}:{k}:{stage}", mode_id,
                                            "invalid_input", (str(exc),), False)
        if feedback.valid and refresh:
            # Reconcile PCC and controller aggregates with this exact executed state.
            net_demand_p[k] = feedback.flow.pcc_import_mw + sum(active.values())
            net_demand_q[k] = feedback.flow.pcc_reactive_mvar + float(np.sum(reactive_actual_by_resource))
        network_records.append(dict(time_minutes=time_minutes, stage=stage, is_actual=refresh, **asdict(feedback)))
        if refresh:
            cycle_network_feedback.append(feedback)
            if not feedback.recovery_safe:
                apply_network_gate(feedback)
        return feedback

    def apply_network_gate(feedback, *, rearm=False):
        rearmed = network_gate.update(feedback, arbiter, device_caps,
                                      {sid: float(pv_actual_by_bus[i]) for i, sid in enumerate(station_ids)},
                                      rearm_requested=rearm)
        if network_gate.blocked:
            caps = {r.station_id: r.effective_cap_mw for r in arbiter.resolve(device_caps)}
            remaining = sum(max(0.0, device_caps[sid] - caps[sid]) for sid in station_ids)
            supervisor.hold_for_network(network_gate.reason, remaining)
        return rearmed

    for k in range(total_steps):
        cycle_network_feedback: list[MinuteNetworkFeedback] = []
        time_minutes = float(k * cfg.device_step_minutes)
        energy_before_step = energy
        storage_before_step = storage_actual
        bounded_wind = limit_synthetic_generation(
            wind_actual, float(wind_avail[k]), float(np.sum(wind_rated))
        )
        wind_availability_limited[k] = max(0.0, wind_actual - bounded_wind)
        wind_actual = bounded_wind
        bounded_pv = np.asarray(
            [
                limit_synthetic_generation(
                    float(value), float(pv_available_by_bus[i, k]), float(pv_rated[i])
                )
                for i, value in enumerate(pv_actual_by_bus)
            ]
        )
        pv_availability_limited[k] = float(np.sum(pv_actual_by_bus - bounded_pv))
        pv_actual_by_bus = bounded_pv
        pv_uncontrolled_actual = np.minimum(pv_uncontrolled_actual, pv_available_by_bus[:, k])
        base_wind_target = min(float(wind_cmd[k]), float(wind_avail[k]))
        base_storage_target = float(storage_cmd[k])
        wind_target = base_wind_target
        storage_target = base_storage_target
        enforcement_decision = None
        if safety_coordinator.active:
            enforcement_snapshot = _synthetic_shancheng_snapshot(
                microgrid=microgrid,
                snapshot_index=k,
                time_minutes=time_minutes,
                step_minutes=cfg.device_step_minutes,
                wind_actual_mw=wind_actual,
                wind_available_mw=float(wind_avail[k]),
                wind_reactive_actual_mvar=float(np.sum(
                    reactive_actual_by_resource[wind_resource_indices]
                )),
                storage_actual_mw=storage_actual,
                storage_energy_mwh=energy_before_step,
                svg_reactive_actual_mvar=float(
                    reactive_actual_by_resource[svg_resource_index]
                ),
                active_feedback=active_actuation_feedback,
                reactive_feedback=reactive_actuation_feedback,
            )
            enforcement_decision = safety_coordinator.enforce(
                enforcement_snapshot,
                decision_id=f"hard-enforce:{microgrid.name}:{k}",
            )
            hard_targets = _active_targets_from_shancheng(enforcement_decision)
            if hard_targets is not None:
                hard_wind_target, hard_storage_target = hard_targets
                # The optimizer may already request a stricter net output.  A
                # hard cap is an upper bound and must never increase it.
                if (
                    hard_wind_target + hard_storage_target
                    <= base_wind_target + base_storage_target + 1e-9
                ):
                    wind_target = hard_wind_target
                    storage_target = hard_storage_target
                    hard_wind_storage_command_accepted[k] = True
                    hard_wind_storage_target[k] = (
                        hard_wind_target + hard_storage_target
                    )
        wind_actual = _approach(wind_actual, wind_target, alpha, p_ramp)
        wind_actual = limit_synthetic_generation(
            wind_actual, float(wind_avail[k]), float(np.sum(wind_rated))
        )
        pv_baseline_target = np.minimum(
            pv_plan_by_bus[:, k],
            pv_available_by_bus[:, k],
        )
        device_caps = {
            station_id: float(pv_baseline_target[index])
            for index, station_id in enumerate(station_ids)
        }
        entry_network = observe_network("pre_response")
        apply_network_gate(entry_network)
        cap_results = arbiter.apply(device_caps)
        cap_by_station = {item.station_id: item.effective_cap_mw for item in cap_results}
        pv_control_target = np.asarray([
            cap_by_station[station_id] for station_id in station_ids
        ])
        for index in range(len(pv_buses)):
            pv_uncontrolled_actual[index] = _approach(
                float(pv_uncontrolled_actual[index]),
                float(pv_baseline_target[index]),
                pv_alpha,
                float(pv_ramp_per_step[index]),
            )
            pv_actual_by_bus[index] = _approach(
                float(pv_actual_by_bus[index]),
                float(pv_control_target[index]),
                pv_alpha,
                float(pv_ramp_per_step[index]),
            )

        storage_actual = _approach(storage_actual, storage_target, alpha, p_ramp)
        storage_actual, energy, storage_soc_within_limits = _storage_response(
            storage_actual,
            energy_before_step,
            microgrid,
            dt_hours,
        )
        if (
            enforcement_decision is not None
            and enforcement_decision.command_id is not None
            and hard_wind_storage_command_accepted[k]
        ):
            active_actuation_feedback = ChannelActuationFeedback(
                execution_known=True,
                last_request_id=enforcement_decision.command_id,
                last_acknowledged_request_id=enforcement_decision.command_id,
            )
        observe_network("plant_response")
        p_grid = float(
            net_demand_p[k]
            - wind_actual
            - float(np.sum(pv_actual_by_bus))
            - storage_actual
        )
        q_grid = float(
            net_demand_q[k] - float(np.sum(reactive_actual_by_resource))
        )

        p_tracking_reference = float(p_cmd[k])
        if supervisor.curtailment_active:
            p_tracking_reference = max(p_tracking_reference, active_safety_floor_mw)
        p_error = p_grid - p_tracking_reference
        correction = float(np.clip(p_error, -p_ramp, p_ramp))
        storage_request = storage_actual + correction
        # Probe the existing PCS/energy model without committing either energy.
        storage_minimum, _, _ = _storage_response(
            -microgrid.storage.p_max_mw, energy_before_step, microgrid, dt_hours
        )
        storage_maximum, _, _ = _storage_response(
            microgrid.storage.p_max_mw, energy_before_step, microgrid, dt_hours
        )
        storage_projection = constrain_storage_power(
            previous_mw=storage_before_step,
            requested_mw=storage_request,
            minimum_mw=storage_minimum,
            maximum_mw=storage_maximum,
            maximum_change_mw=p_ramp,
        )
        storage_actual, energy, correction_within_soc = _storage_response(
            storage_projection.actual_mw,
            energy_before_step,
            microgrid,
            dt_hours,
        )
        storage_soc_within_limits = (
            storage_soc_within_limits and correction_within_soc
        )
        decision_network = observe_network("pre_control")
        network_rearmed = apply_network_gate(decision_network, rearm=k in network_rearm_steps)
        q_grid = float(net_demand_q[k] - float(np.sum(reactive_actual_by_resource)))
        p_grid = float(
            net_demand_p[k]
            - wind_actual
            - float(np.sum(pv_actual_by_bus))
            - storage_actual
        )

        group_caps = arbiter.owner_caps("group_control")
        curtailment_ledger.synchronize(device_caps, group_caps)
        supervisor_owned_curtailment = curtailment_ledger.remaining_mw(group_caps)
        exogenous_net_load = float(
            net_demand_p[k]
            - wind_actual
            - float(np.sum(pv_uncontrolled_actual))
        )
        if k == 0:
            current_net_load_feature = exogenous_net_load
            previous_net_load_feature = exogenous_net_load
        elif k % feature_steps == 0:
            previous_net_load_feature = current_net_load_feature
            current_net_load_feature = exogenous_net_load

        apparent_before_q_control = float(np.hypot(p_grid, q_grid))
        pf_before_q_control = (
            abs(p_grid) / apparent_before_q_control
            if apparent_before_q_control > 1e-9
            else 1.0
        )
        cap_states = arbiter.resolve(device_caps)
        restoration_observation = (
            RestorationObservation(
                time_minutes=time_minutes,
                stations=tuple(
                    RestorationStation(
                        station_id=sid,
                        actual_mw=float(pv_actual_by_bus[i]),
                        baseline_mw=float(pv_plan_by_bus[i, k]),
                        available_mw=float(pv_available_by_bus[i, k]),
                        other_caps=tuple(
                            (owner, cap)
                            for owner, cap in cap_states[i].owner_caps
                            if owner != "group_control"
                        ),
                    )
                    for i, sid in enumerate(station_ids)
                ),
                valid=decision_network.valid and not network_gate.blocked,
                acknowledged_command_id=restoration_command_id,
            )
            if station_ids
            else None
        )
        response_evidence = (
            restoration_monitor.observe(restoration_observation)
            if restoration_observation is not None
            else None
        )
        if response_evidence is not None:
            restoration_evidence.append(response_evidence)
        achieved_restoration = (
            response_evidence.achieved_mw if response_evidence is not None else None
        )

        interval_index = min(T - 1, k // substeps)
        interval_start_minutes = float(interval_index * minutes_per_interval)
        if reverse_flow_probability_15 is None:
            reverse_risk = ReverseFlowRiskEstimate(
                probability=None,
                forecast_as_of_minutes=interval_start_minutes,
                horizon_minutes=group_cfg.risk_probability_horizon_minutes,
                valid_until_minutes=interval_start_minutes + minutes_per_interval,
                validity=SignalValidity.invalid_signal(),
            )
        else:
            reverse_risk = ReverseFlowRiskEstimate(
                probability=float(reverse_flow_probability_15[interval_index]),
                forecast_as_of_minutes=interval_start_minutes,
                horizon_minutes=group_cfg.risk_probability_horizon_minutes,
                valid_until_minutes=interval_start_minutes + minutes_per_interval,
                validity=SignalValidity.valid_signal(),
            )
        valid_signal = SignalValidity.valid_signal()
        station_snapshots = tuple(
            PVStationSnapshot(
                station_id=station_id,
                bus=pv_buses[index],
                measured_power_mw=float(pv_actual_by_bus[index]),
                available_power_mw=float(pv_available_by_bus[index, k]),
                minimum_power_mw=0.0,
                ramp_down_mw_per_minute=float(pv_ramp_per_minute[index]),
                ramp_up_mw_per_minute=float(pv_ramp_per_minute[index]),
                in_service=True,
                healthy=True,
                communication_ok=True,
                controllable=True,
                measured_power_validity=valid_signal,
                available_power_validity=valid_signal,
                status_validity=valid_signal,
                actuation_feedback=StationActuationFeedback.synthetic_success(),
            )
            for index, station_id in enumerate(station_ids)
        )
        controllable_pv_available = (
            bool(pv_buses)
            and float(np.sum(pv_available_by_bus[:, k])) > 1e-9
        )
        decision = supervisor.step(
            GroupTelemetrySnapshot(
                snapshot_id=f"{microgrid.name}:{k}",
                time_minutes=time_minutes,
                coherent=decision_network.valid,
                pcc_power_mw=p_grid,
                current_net_load_mw=current_net_load_feature,
                previous_net_load_mw=previous_net_load_feature,
                maximum_load_mw=microgrid.maximum_load_mw,
                pv_capacity_mw=installed_pv_total,
                p_grid_max_mw=microgrid.p_grid_max_mw,
                pv_actual_mw=float(np.sum(pv_actual_by_bus)),
                reverse_flow_risk=reverse_risk,
                pcc_validity=decision_network.validity,
                load_validity=decision_network.validity,
                pv_aggregate_validity=valid_signal,
                network_limits_validity=decision_network.validity,
                hard_protection=HardProtectionStatus(hard_protection_latched, valid_signal),
                stations=station_snapshots,
                measured_supervisor_curtailment_mw=supervisor_owned_curtailment,
                achieved_restoration_mw=achieved_restoration,
                recovery_command_acknowledged=True,
                recovery_command_valid=(
                    not network_gate.blocked
                    and (response_evidence is None or response_evidence.status != "unknown")
                ),
                recovery_rearm_requested=network_rearmed,
                voltage_within_limits=decision_network.voltage_within_limits is True,
                line_capacity_within_limits=decision_network.line_capacity_within_limits is True,
                controllable_pv_available=controllable_pv_available,
                power_factor_within_limits=(pf_before_q_control >= case.assumptions.pf_min - 1e-8),
                storage_soc_within_limits=storage_soc_within_limits,
            )
        )
        active_safety_floor_mw = decision.thresholds.safety_threshold_mw

        allocation_unserved = 0.0
        if decision.action is GroupControlAction.CURTAIL:
            allocation = allocate_pv_curtailment(
                decision.requested_curtailment_mw,
                station_snapshots,
                decision_interval_minutes=cfg.device_step_minutes,
            )
            existing_group_caps = arbiter.owner_caps("group_control")
            group_intents = tuple(
                StationCapIntent(
                    owner_id="group_control",
                    station_id=station_ids[index],
                    action=CapIntentAction.SET_CAP,
                    absolute_cap_mw=min(
                        existing_group_caps.get(
                            station_ids[index], float("inf")
                        ),
                        command.target_power_mw,
                    ),
                    decision_id=f"group-curtail:{k}",
                )
                for index, command in enumerate(allocation.commands)
                if command.allocated_curtailment_mw > 1e-12
            )
            arbiter.apply(device_caps, group_intents)
            allocation_unserved = allocation.unserved_curtailment_mw
            curtailment_ledger.synchronize(
                device_caps, arbiter.owner_caps("group_control"), new_curtailment=True
            )
            restoration_monitor.cancel()
            restoration_command_id = None
            group_curtailment_commands += 1
        elif decision.action is GroupControlAction.RESTORE and not network_gate.blocked:
            restoration_command_id = f"group-restore:{microgrid.name}:{k}"
            owned_references = curtailment_ledger.references()
            restoration_intents = _release_group_caps(
                arbiter,
                station_ids,
                np.asarray([owned_references.get(sid, device_caps[sid]) for sid in station_ids]),
                decision.requested_restoration_mw,
                decision_id=restoration_command_id,
            )
            arbiter.apply(device_caps, restoration_intents)
            assert restoration_observation is not None
            restoration_monitor.begin(
                RestorationCommand(
                    restoration_command_id,
                    decision.requested_restoration_mw,
                    max(
                        0.0,
                        supervisor_owned_curtailment
                        - curtailment_ledger.remaining_mw(arbiter.owner_caps("group_control")),
                    ),
                    restoration_observation,
                )
            )
            group_restoration_commands += 1
        elif decision.action is GroupControlAction.ABORT_RECOVERY:
            restoration_monitor.cancel()
            restoration_command_id = None
            group_recovery_aborts += 1
        elif decision.state is not GroupControlState.RESTORING:
            restoration_monitor.cancel()
            restoration_command_id = None
        if not supervisor.curtailment_active and not network_gate.blocked:
            existing_group_caps = arbiter.owner_caps("group_control")
            arbiter.apply(
                device_caps,
                tuple(StationCapIntent(
                    owner_id="group_control",
                    station_id=station_id,
                    action=CapIntentAction.RELEASE_CAP,
                    decision_id=f"group-release:{k}",
                ) for station_id in existing_group_caps),
            )

        cap_results = arbiter.apply(device_caps)
        cap_by_station = {item.station_id: item.effective_cap_mw for item in cap_results}
        pv_control_target = np.asarray([
            cap_by_station[station_id] for station_id in station_ids
        ])

        pcc_before_local_safety[k] = p_grid
        reverse_reduction_mw = 0.0
        safety_unserved_mw = 0.0
        if p_grid < 0.0:
            before_violations += 1
        if p_grid < case.assumptions.no_reverse_margin_mw:
            hard_release_guard.reset()
            pcc_before_hard = p_grid
            deficit = case.assumptions.no_reverse_margin_mw - p_grid
            pv_before_hard = float(np.sum(pv_actual_by_bus))
            hard_target = pv_actual_by_bus.copy()
            _proportional_reduce(hard_target, deficit)
            hard_intents = tuple(
                StationCapIntent(
                    owner_id="hard_protection",
                    station_id=station_ids[index],
                    action=CapIntentAction.SET_CAP,
                    absolute_cap_mw=float(hard_target[index]),
                    decision_id=f"hard-curtail:{k}",
                )
                for index in range(len(station_ids))
                if hard_target[index] < pv_actual_by_bus[index] - 1e-12
            )
            hard_results = arbiter.apply(device_caps, hard_intents)
            hard_cap_by_station = {
                item.station_id: item.effective_cap_mw for item in hard_results
            }
            if station_ids:
                # Synthetic plant response: an emergency absolute cap is
                # executed by the PV adapter, not by either intent owner.
                pv_actual_by_bus = np.minimum(
                    pv_actual_by_bus,
                    np.asarray([
                        hard_cap_by_station[station_id]
                        for station_id in station_ids
                    ]),
                )
            pv_reduction = max(0.0, pv_before_hard - float(np.sum(pv_actual_by_bus)))
            hard_pv_network = observe_network("hard_pv_response")
            p_grid_after_pv = float(
                net_demand_p[k]
                - wind_actual
                - float(np.sum(pv_actual_by_bus))
                - storage_actual
            )

            safety_snapshot = _synthetic_shancheng_snapshot(
                microgrid=microgrid,
                snapshot_index=k,
                time_minutes=time_minutes,
                step_minutes=cfg.device_step_minutes,
                wind_actual_mw=wind_actual,
                wind_available_mw=float(wind_avail[k]),
                wind_reactive_actual_mvar=float(np.sum(
                    reactive_actual_by_resource[wind_resource_indices]
                )),
                storage_actual_mw=storage_actual,
                # The emergency proposal replaces the preliminary storage
                # request for this device step, so both capability planning
                # and final energy integration use the same pre-step state.
                storage_energy_mwh=energy_before_step,
                svg_reactive_actual_mvar=float(
                    reactive_actual_by_resource[svg_resource_index]
                ),
                active_feedback=active_actuation_feedback,
                reactive_feedback=reactive_actuation_feedback,
            )
            safety_allocation, safety_decision = safety_coordinator.allocate(
                safety_snapshot,
                PccSafetyConstraint(
                    owner_id="hard_protection",
                    # Reducing generation can also reduce I²R losses. Reserve the
                    # currently computed nonnegative loss so the old loss value is
                    # not incorrectly counted as guaranteed post-action import.
                    minimum_import_mw=(case.assumptions.no_reverse_margin_mw
                                       + (hard_pv_network.flow.loss_mw if hard_pv_network.valid else 0.0)),
                    decision_id=f"hard-wind-storage:{microgrid.name}:{k}",
                ),
                pcc_after_confirmed_pv_mw=p_grid_after_pv,
                confirmed_pv_reduction_mw=pv_reduction,
            )
            hard_actuation = _apply_synthetic_shancheng_active_decision(
                safety_decision,
                energy_before_mwh=energy_before_step,
                microgrid=microgrid,
                dt_hours=dt_hours,
            )
            if safety_allocation.command_accepted and hard_actuation is not None:
                wind_actual = hard_actuation.wind_actual_mw
                storage_actual = hard_actuation.storage_actual_mw
                energy = hard_actuation.storage_energy_mwh
                storage_soc_within_limits = (
                    storage_soc_within_limits
                    and hard_actuation.storage_soc_within_limits
                )
                active_actuation_feedback = hard_actuation.feedback
                hard_wind_storage_command_accepted[k] = True
                hard_wind_storage_target[k] = (
                    wind_actual + storage_actual
                )

            # Only executed plant values may change the measured PCC.  A
            # requested reduction is never added algebraically to telemetry.
            observe_network("hard_wind_storage_response")
            p_grid = float(
                net_demand_p[k]
                - wind_actual
                - float(np.sum(pv_actual_by_bus))
                - storage_actual
            )
            reverse_reduction_mw = max(0.0, p_grid - pcc_before_hard)
            safety_unserved_mw = max(
                0.0,
                case.assumptions.no_reverse_margin_mw - p_grid,
            )
            local_reverse_interventions += 1
            hard_protection_latched = (
                bool(arbiter.owner_caps("hard_protection"))
                or safety_coordinator.active
            )
        elif hard_protection_latched:
            # 在不改变仲裁器状态的前提下，评估“下一周期释放硬保护”是否仍安全。
            # 只有反事实PCC连续满足安全裕度+迟滞，才在本周期末显式释放；
            # 因而监督器本周期仍看到 HARD_OVERRIDE，不会同周期恢复群控上限。
            without_hard = arbiter.resolve(
                device_caps,
                excluded_owner_ids=("hard_protection",),
            )
            without_hard_by_station = {
                item.station_id: item.effective_cap_mw for item in without_hard
            }
            counterfactual_pv = np.asarray([
                _approach(
                    float(pv_actual_by_bus[index]),
                    without_hard_by_station[station_id],
                    pv_alpha,
                    float(pv_ramp_per_step[index]),
                )
                for index, station_id in enumerate(station_ids)
            ])
            counterfactual_wind = _approach(
                wind_actual,
                base_wind_target,
                alpha,
                p_ramp,
            )
            counterfactual_storage_request = _approach(
                storage_actual,
                base_storage_target,
                alpha,
                p_ramp,
            )
            counterfactual_storage, _, _ = _storage_response(
                counterfactual_storage_request,
                energy,
                microgrid,
                dt_hours,
            )
            candidate_network = observe_network("hard_release_candidate", pv=counterfactual_pv,
                                                wind=counterfactual_wind, storage=counterfactual_storage,
                                                refresh=False)
            counterfactual_p_grid = candidate_network.flow.pcc_import_mw if candidate_network.valid else None
            if not candidate_network.recovery_safe:
                hard_release_guard.reset()
            if candidate_network.recovery_safe and not network_gate.blocked and hard_release_guard.observe(
                counterfactual_p_grid,
                case.assumptions.no_reverse_margin_mw,
            ):
                arbiter.apply(
                    device_caps,
                    tuple(StationCapIntent(
                        owner_id="hard_protection",
                        station_id=station_id,
                        action=CapIntentAction.RELEASE_CAP,
                        decision_id=f"hard-release-confirmed:{k}",
                    ) for station_id in arbiter.owner_caps("hard_protection")),
                )
                safety_coordinator.release()
                hard_protection_latched = False
                hard_release_guard.reset()
        hard_safety_unserved[k] = safety_unserved_mw
        final_cap_results = arbiter.apply(device_caps)
        final_cap_by_station = {
            item.station_id: item.effective_cap_mw for item in final_cap_results
        }
        pv_control_target = np.asarray([
            final_cap_by_station[station_id] for station_id in station_ids
        ])

        wind_active_by_resource = _allocate_nonnegative_total(
            wind_actual,
            wind_command_by_resource[:, k],
            wind_available_by_resource[:, k],
            wind_resource_ids,
        )
        active_by_resource = {
            item.resource_id: 0.0 for item in resource_schedules
        }
        for index, schedule in enumerate(wind_schedules):
            active_by_resource[schedule.resource_id] = float(
                wind_active_by_resource[index]
            )
        for index, schedule in enumerate(pv_schedules):
            active_by_resource[schedule.resource_id] = float(
                pv_actual_by_bus[index]
            )
        active_by_resource[storage_schedules[0].resource_id] = abs(storage_actual)

        reactive_capabilities = calculate_reactive_capabilities(
            microgrid,
            resource_schedules,
            active_by_resource,
        )
        # The optimizer, prediction and command projection share all physical
        # envelopes; field kP limits never replace converter MVA limits.
        adjusted_capabilities = []
        for schedule, capability in zip(resource_schedules, reactive_capabilities):
            lower, upper = capability.minimum_mvar, capability.maximum_mvar
            if schedule.resource_type is ResourceType.WIND:
                bound = wind_reactive_ratio * active_by_resource[schedule.resource_id]
                lower, upper = max(lower, -bound), min(upper, bound)
            if schedule.resource_type is ResourceType.SVG:
                lower, upper = max(lower, -svg_dispatch_limit), min(upper, svg_dispatch_limit)
            adjusted_capabilities.append(ReactiveCapability(schedule.resource_id, lower, upper))
        reactive_capabilities = tuple(adjusted_capabilities)
        capability_by_resource = {item.resource_id: item for item in reactive_capabilities}
        lower_q = np.array([c.minimum_mvar for c in reactive_capabilities])
        upper_q = np.array([c.maximum_mvar for c in reactive_capabilities])
        reactive_actual_by_resource = np.clip(reactive_actual_by_resource, lower_q, upper_q)
        previous_reactive_actual = reactive_actual_by_resource.copy()
        # Refresh after active actions AND capability clipping, never reuse
        # pre-action AC losses to certify the eventual PF boundary.
        reactive_feedback = observe_network("pre_reactive")
        q_before_control = float(net_demand_q[k] - np.sum(previous_reactive_actual))
        p_before_reactive = (reactive_feedback.flow.pcc_import_mw
                             if reactive_feedback.valid else p_grid)
        q_limit = max(0.0, pf_tan * p_before_reactive)
        desired_q_grid = float(np.clip(float(q_cmd[k]), -q_limit, q_limit))
        desired_reactive_total = float(net_demand_q[k] - desired_q_grid)
        allocation = allocate_bounded_reactive_power(
            desired_reactive_total,
            {rid: float(q_plan_by_resource[i, k]) for i, rid in enumerate(reactive_resource_ids)},
            reactive_capabilities,
        )
        preferred = np.array([allocation.target_by_resource[rid] for rid in reactive_resource_ids])
        reactive_snapshot = _synthetic_shancheng_snapshot(
            microgrid=microgrid, snapshot_index=k, time_minutes=time_minutes,
            step_minutes=cfg.device_step_minutes,
            wind_actual_mw=wind_actual, wind_available_mw=float(wind_avail[k]),
            wind_reactive_actual_mvar=float(np.sum(previous_reactive_actual[wind_resource_indices])),
            storage_actual_mw=storage_actual, storage_energy_mwh=energy,
            svg_reactive_actual_mvar=float(previous_reactive_actual[svg_resource_index]),
            active_feedback=active_actuation_feedback, reactive_feedback=reactive_actuation_feedback,
        )
        # One sequence/message per real cycle. Previews clone the controller;
        # they neither acknowledge commands nor consume its replay state.
        command_template = reactive_command_factory.make(
            now_utc=reactive_snapshot.observed_at_utc,
            message_id=f"reactive-plan:{microgrid.name}:{k}", reactive_target_mvar=0.0,
        )
        def command_for(controls):
            total = float(np.sum(controls[wind_resource_indices]) + controls[svg_resource_index])
            return replace(command_template, reactive_request=replace(
                command_template.reactive_request, target_mvar=total))

        def targets_from_decision(controls, local_decision):
            targets = np.clip(controls.copy(), lower_q, upper_q)
            writes = _reactive_targets_from_shancheng(local_decision)
            if (local_decision.reactive_disposition is not CommandDisposition.ACCEPTED
                    or writes is None):
                targets[wind_resource_indices] = previous_reactive_actual[wind_resource_indices]
                targets[svg_resource_index] = previous_reactive_actual[svg_resource_index]
                return targets
            wind_q, svg_q = writes
            wind_allocation = allocate_bounded_reactive_power(
                wind_q,
                {s.resource_id: float(controls[schedule_index[s.resource_id]]) for s in wind_schedules},
                tuple(capability_by_resource[s.resource_id] for s in wind_schedules),
            ).target_by_resource
            for schedule in wind_schedules:
                targets[schedule_index[schedule.resource_id]] = wind_allocation[schedule.resource_id]
            targets[svg_resource_index] = np.clip(svg_q, lower_q[svg_resource_index], upper_q[svg_resource_index])
            return targets

        def project_targets(controls):
            proposal = deepcopy(shancheng_controller).step(reactive_snapshot, command_for(controls))
            return targets_from_decision(controls, proposal)

        correction = None
        reactive_decision = None
        reactive_targets = previous_reactive_actual.copy()
        snapshot_matches_execution = reactive_feedback.valid and all(
            d["in_service"]
            and abs(d["p_mw"] - (storage_actual if d["resource_id"] == storage_schedules[0].resource_id
                                else active_by_resource[d["resource_id"]])) <= 1e-7
            and abs(d["q_mvar"] - previous_reactive_actual[schedule_index[d["resource_id"]]]) <= 1e-7
            for d in reactive_feedback.inputs["snapshot"]["devices"])
        if snapshot_matches_execution and reactive_actuation_feedback.execution_known:
            network = assess_network_model(microgrid.network_model_v2, mode_id).require_current_solver_ready()
            bus_ids = [b.bus_id for b in network.buses]
            safety_limits = replace(evaluator.limits,
                pcc_import_min_mw=max(evaluator.limits.pcc_import_min_mw, case.assumptions.no_reverse_margin_mw),
                power_factor_min=max(evaluator.limits.power_factor_min, case.assumptions.pf_min))
            correction = correct_reactive_dispatch(
                network, safety_limits,
                p_demand_mw=np.array([reactive_feedback.inputs["p_demand_mw_by_bus"][b] for b in bus_ids]),
                q_demand_before_mvar=np.array([reactive_feedback.inputs["q_demand_mvar_by_bus"][b] for b in bus_ids]),
                resource_buses=[s.bus_id for s in resource_schedules],
                current_q=previous_reactive_actual, preferred_targets=preferred,
                capabilities=reactive_capabilities, project_targets=project_targets,
                slack_voltage_pu=reactive_feedback.inputs["snapshot"]["slack_voltage_pu"],
                alpha=alpha, ramp_mvar=q_ramp,
            )
            if correction.status in ("safe", "corrected", "best_effort"):
                reactive_decision = shancheng_controller.step(reactive_snapshot, command_for(correction.controls))
                reactive_targets = targets_from_decision(correction.controls, reactive_decision)
                # Commit precisely the predicted/arbitrated device targets.
                # Any discrepancy is a held execution, never an assumed success.
                if not np.allclose(reactive_targets, correction.targets, atol=1e-9, rtol=0):
                    reactive_targets = previous_reactive_actual.copy()
        reactive_actual_by_resource = _approach_reactive_vector(
            previous_reactive_actual, reactive_targets, alpha=alpha, aggregate_ramp_mvar=q_ramp)
        command_accepted = (reactive_decision is not None and
            reactive_decision.reactive_disposition is CommandDisposition.ACCEPTED)
        shancheng_reactive_command_accepted[k] = command_accepted
        if command_accepted and reactive_decision.command_id is not None:
            reactive_actuation_feedback = ChannelActuationFeedback(
                execution_known=True, last_request_id=reactive_decision.command_id,
                last_acknowledged_request_id=reactive_decision.command_id)
        reactive_execution_known[k] = reactive_actuation_feedback.execution_known
        pf_adjustment_mvar = 0.0
        if abs(q_before_control) > q_limit + 1e-9:
            applied = float(np.sum(reactive_actual_by_resource - previous_reactive_actual))
            pf_adjustment_mvar = float(np.sign(q_before_control) * max(0., np.sign(q_before_control)*applied))
            local_pf_interventions += 1
        final_network = observe_network("post_control")
        apply_network_gate(final_network)
        network_blocked[k] = network_gate.blocked
        network_invalid_steps += int(any(not item.valid for item in cycle_network_feedback))
        network_violation_steps += int(any(item.valid and not item.recovery_safe for item in cycle_network_feedback))
        if final_network.valid:
            p_grid = final_network.flow.pcc_import_mw
            q_grid = final_network.flow.pcc_reactive_mvar
        else:
            p_grid = q_grid = float("nan")
        # Freeze future targets without rewriting already executed plant values.
        post_caps = {r.station_id: r.effective_cap_mw for r in arbiter.resolve(device_caps)}
        pv_control_target = np.asarray([post_caps[sid] for sid in station_ids])
        apparent = float(np.hypot(p_grid, q_grid))
        pf = (abs(p_grid) / apparent if apparent > 1e-9 else 1.0) if final_network.valid else float("nan")
        if final_network.valid and p_grid < -1e-8:
            after_violations += 1
        if pf < case.assumptions.pf_min - 1e-8:
            pf_violations += 1

        final_q_limit = max(0., p_grid) * tan(acos(max(
            case.assumptions.pf_min, evaluator.limits.power_factor_min))) if final_network.valid else 0.
        reactive_dispatch_unserved[k] = (
            max(0., abs(q_grid) - final_q_limit) if final_network.valid else 0.)
        group_q_unserved = 0.
        if reactive_decision is not None:
            requested_group_q = float(np.sum(correction.controls[wind_resource_indices])
                                      + correction.controls[svg_resource_index])
            retained_group_q = float(np.sum(reactive_targets[wind_resource_indices])
                                     + reactive_targets[svg_resource_index])
            group_q_unserved = abs(requested_group_q - retained_group_q)
            reactive_dispatch_unserved[k] = max(reactive_dispatch_unserved[k], group_q_unserved)
        network_records[-1]["reactive_control"] = {
            "status": correction.status if correction is not None else "unavailable",
            "candidate_evaluations": correction.evaluations if correction is not None else 0,
            "predicted_safe": correction is not None and correction.status in ("safe", "corrected"),
            "post_action_safe": final_network.valid and not final_network.flow.violations,
            "command_accepted": command_accepted,
            "snapshot_matches_execution": bool(snapshot_matches_execution),
            "group_target_unserved_mvar": group_q_unserved,
            "pf_unserved_mvar": float(reactive_dispatch_unserved[k]) if final_network.valid else None,
            "resource_ids": list(reactive_resource_ids),
            "q_before_mvar": previous_reactive_actual.tolist(),
            "q_targets_mvar": reactive_targets.tolist(),
            "q_executed_mvar": reactive_actual_by_resource.tolist(),
            "aggregate_q_ramp_mvar": q_ramp,
        }

        p_actual[k] = p_grid
        q_actual[k] = q_grid
        wind_actual_series[k] = wind_actual
        wind_resource_actual[:, k] = wind_active_by_resource
        pv_actual_series[k] = float(np.sum(pv_actual_by_bus))
        pv_control_target_series[k] = float(np.sum(pv_control_target))
        pv_actual_by_bus_series[:, k] = pv_actual_by_bus
        pv_control_target_by_bus_series[:, k] = pv_control_target
        storage_actual_series[k] = storage_actual
        storage_dynamics.append(
            StorageDynamicsRecord(
                time_minutes=time_minutes,
                previous_mw=storage_before_step,
                requested_mw=storage_request,
                ordinary_mw=storage_projection.actual_mw,
                actual_mw=storage_actual,
                maximum_change_mw=p_ramp,
                ordinary_unserved_mw=storage_projection.unserved_mw,
                physical_override=storage_projection.physical_override,
                hard_override=bool(hard_wind_storage_command_accepted[k]),
            )
        )
        reactive_target_by_resource_series[:, k] = reactive_targets
        reactive_actual_by_resource_series[:, k] = reactive_actual_by_resource
        storage_energy_series[k] = energy
        storage_soc_compliant_series[k] = storage_soc_within_limits
        actual_power_factor[k] = pf
        local_reverse_reduction[k] = reverse_reduction_mw
        group_hard_override_active[k] = decision.hard_protection_active
        local_pf_adjustment[k] = pf_adjustment_mvar
        group_states[k] = decision.state.value
        group_actions[k] = decision.action.value
        group_reasons[k] = decision.trigger_reason
        group_risk_limit[k] = decision.thresholds.risk_limit_mw
        group_safety_threshold[k] = decision.thresholds.safety_threshold_mw
        group_restore_threshold[k] = decision.thresholds.restore_threshold_mw
        group_current_net_load[k] = current_net_load_feature
        group_load_change_rate[k] = decision.thresholds.load_change_rate
        group_pv_penetration[k] = decision.thresholds.pv_penetration
        group_reverse_flow_probability[k] = decision.thresholds.reverse_flow_probability
        observed_probability = decision.thresholds.observed_reverse_flow_probability
        if observed_probability is not None:
            group_observed_reverse_flow_probability[k] = observed_probability
        group_reverse_flow_probability_valid[k] = (
            decision.thresholds.reverse_flow_probability_valid
        )
        group_risk_index[k] = decision.thresholds.risk_index
        group_required_curtailment[k] = decision.required_curtailment_mw
        group_requested_curtailment[k] = decision.requested_curtailment_mw
        group_unserved_curtailment[k] = (
            decision.unserved_curtailment_mw
            + allocation_unserved
            + safety_unserved_mw
        )
        group_requested_restoration[k] = decision.requested_restoration_mw
        group_achieved_restoration[k] = (
            float(achieved_restoration) if achieved_restoration is not None else float("nan")
        )
        group_remaining_curtailment[k] = decision.remaining_curtailment_mw
        group_recovery_dwell_remaining[k] = decision.recovery_dwell_remaining_minutes
        if decision.recovery_evaluation_passed is not None:
            group_recovery_evaluation_passed[k] = float(
                decision.recovery_evaluation_passed
            )
        group_restoration_response_error[k] = decision.restoration_response_error_mw
        measured_pv_curtailment[k] = float(np.sum(np.maximum(
            0.0,
            pv_uncontrolled_actual - pv_actual_by_bus,
        )))

    return DeviceTrackingResult(
        name=microgrid.name,
        time_minutes=np.arange(total_steps, dtype=float) * cfg.device_step_minutes,
        pcc_command_mw=p_cmd,
        pcc_actual_mw=p_actual,
        qcc_command_mvar=q_cmd,
        qcc_actual_mvar=q_actual,
        p_tracking_rmse_mw=float(np.sqrt(np.mean((p_actual - p_cmd) ** 2))),
        q_tracking_rmse_mvar=float(np.sqrt(np.mean((q_actual - q_cmd) ** 2))),
        no_reverse_violations_before_safety=before_violations,
        no_reverse_violations_after_safety=after_violations,
        pf_violations_after_safety=pf_violations,
        safety_interventions=(local_reverse_interventions + local_pf_interventions),
        network_feedback=network_records,
        network_context=dict(
            network=asdict(microgrid.network_model_v2) if microgrid.network_model_v2 else None,
            operating_mode_id=mode_id,
            limits=asdict(evaluator.limits),
            config=asdict(minute_network_config),
            slack_voltage_pu=slack_voltage_pu,
            source="parameterized SIL minute plant; not field validation",
        ),
        network_security_passed=bool(network_records)
        and network_invalid_steps == 0
        and network_violation_steps == 0,
        network_invalid_steps=network_invalid_steps,
        network_violation_steps=network_violation_steps,
        network_recovery_blocked=network_blocked,
        wind_command_mw=wind_cmd,
        wind_available_mw=wind_avail,
        wind_actual_mw=wind_actual_series,
        execution_evidence_version="minute-execution-v2",
        wind_resource_actual_mw={
            rid: wind_resource_actual[i].copy() for i, rid in enumerate(wind_resource_ids)
        },
        wind_availability_limited_mw=wind_availability_limited,
        pv_availability_limited_mw=pv_availability_limited,
        group_restoration_evidence=tuple(restoration_evidence),
        pv_plan_mw=np.repeat(pv_command_15, substeps),
        pv_available_mw=np.sum(pv_available_by_bus, axis=0),
        pv_control_target_mw=pv_control_target_series,
        pv_actual_mw=pv_actual_series,
        pv_station_plan_mw={
            bus: pv_plan_by_bus[index].copy() for index, bus in enumerate(pv_buses)
        },
        pv_station_available_mw={
            bus: pv_available_by_bus[index].copy() for index, bus in enumerate(pv_buses)
        },
        pv_station_control_target_mw={
            bus: pv_control_target_by_bus_series[index].copy() for index, bus in enumerate(pv_buses)
        },
        pv_station_actual_mw={
            bus: pv_actual_by_bus_series[index].copy() for index, bus in enumerate(pv_buses)
        },
        storage_actual_mw=storage_actual_series,
        storage_dynamics=tuple(storage_dynamics),
        reactive_resource_plan_mvar={
            resource_id: q_plan_by_resource[index].copy()
            for index, resource_id in enumerate(reactive_resource_ids)
        },
        reactive_resource_target_mvar={
            resource_id: reactive_target_by_resource_series[index].copy()
            for index, resource_id in enumerate(reactive_resource_ids)
        },
        reactive_resource_actual_mvar={
            resource_id: reactive_actual_by_resource_series[index].copy()
            for index, resource_id in enumerate(reactive_resource_ids)
        },
        wind_reactive_actual_mvar=np.sum(
            reactive_actual_by_resource_series[wind_resource_indices], axis=0
        ),
        pv_reactive_actual_mvar=(
            np.sum(reactive_actual_by_resource_series[pv_resource_indices], axis=0)
            if pv_resource_indices.size
            else np.zeros(total_steps)
        ),
        storage_reactive_actual_mvar=(
            reactive_actual_by_resource_series[storage_resource_index].copy()
        ),
        svg_reactive_actual_mvar=(reactive_actual_by_resource_series[svg_resource_index].copy()),
        reactive_dispatch_unserved_mvar=reactive_dispatch_unserved,
        shancheng_reactive_command_accepted=(shancheng_reactive_command_accepted),
        reactive_execution_known=reactive_execution_known,
        hard_wind_storage_target_mw=hard_wind_storage_target,
        hard_safety_unserved_mw=hard_safety_unserved,
        hard_wind_storage_command_accepted=(hard_wind_storage_command_accepted),
        group_control_state=np.asarray(group_states, dtype=str),
        group_control_action=np.asarray(group_actions, dtype=str),
        group_control_reason=np.asarray(group_reasons, dtype=str),
        group_risk_limit_mw=group_risk_limit,
        group_safety_threshold_mw=group_safety_threshold,
        group_restore_threshold_mw=group_restore_threshold,
        group_current_net_load_mw=group_current_net_load,
        group_load_change_rate=group_load_change_rate,
        group_pv_penetration=group_pv_penetration,
        group_reverse_flow_probability=group_reverse_flow_probability,
        group_observed_reverse_flow_probability=(group_observed_reverse_flow_probability),
        group_reverse_flow_probability_valid=(group_reverse_flow_probability_valid),
        group_risk_index=group_risk_index,
        group_required_curtailment_mw=group_required_curtailment,
        group_requested_curtailment_mw=group_requested_curtailment,
        group_unserved_curtailment_mw=group_unserved_curtailment,
        group_requested_restoration_mw=group_requested_restoration,
        group_achieved_restoration_mw=group_achieved_restoration,
        group_remaining_curtailment_mw=group_remaining_curtailment,
        group_recovery_dwell_remaining_minutes=group_recovery_dwell_remaining,
        group_recovery_evaluation_passed=group_recovery_evaluation_passed,
        group_restoration_response_error_mw=group_restoration_response_error,
        measured_pv_curtailment_mw=measured_pv_curtailment,
        pcc_before_local_safety_mw=pcc_before_local_safety,
        local_reverse_flow_reduction_mw=local_reverse_reduction,
        group_hard_override_active=group_hard_override_active,
        local_power_factor_adjustment_mvar=local_pf_adjustment,
        actual_power_factor=actual_power_factor,
        storage_energy_mwh=storage_energy_series,
        storage_soc_within_limits=storage_soc_compliant_series,
        group_control_events=supervisor.events,
        group_curtailment_commands=group_curtailment_commands,
        group_restoration_commands=group_restoration_commands,
        group_recovery_aborts=group_recovery_aborts,
        local_reverse_flow_interventions=local_reverse_interventions,
        local_power_factor_interventions=local_pf_interventions,
    )
