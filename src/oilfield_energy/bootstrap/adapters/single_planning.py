"""Projection of the single-region economic planning request."""

from dataclasses import replace

from oilfield_energy.data import ProjectCase
from oilfield_energy.modules.control.contracts import (
    DeviceCheckpoint,
    DynamicTrackingPolicy,
)
from oilfield_energy.modules.dispatch.contracts import (
    ComputationQualityPolicy,
    PQPlanningInput,
    ReactivePlanningPolicy,
)
from oilfield_energy.workflows.cluster_execution.contracts import ExecutionPolicy

from .cluster_reactive import with_reactive_planning


def single_execution_policy(
    quality: ComputationQualityPolicy | None = None,
) -> ExecutionPolicy:
    """Record common physical rules without enabling cluster coordination policies."""
    return ExecutionPolicy(
        dynamic_tracking=DynamicTrackingPolicy(),
        reactive_planning=ReactivePlanningPolicy(),
        computation_quality=quality,
        device_capability_version="device-capability-v2",
        single_execution_version="unified-single-v7",
    )


def prepare_single_case(
    case: ProjectCase,
    region: str,
    *,
    storage_enabled: bool,
    policy: ExecutionPolicy,
    checkpoint: DeviceCheckpoint | None = None,
    joint_pq: PQPlanningInput | None = None,
) -> ProjectCase:
    """Select one region and build V2 rows from the declared completed state."""
    if policy.dynamic_tracking is None or policy.reactive_planning is None:
        raise ValueError(
            "independent V2 execution requires explicit planning and dynamics"
        )
    if policy.reactive_planning.version != "reactive-planning-v2":
        raise ValueError(
            "independent V2 execution requires the corrected reachability envelope"
        )
    grids = [mg for mg in case.microgrids if mg.name == region]
    if len(grids) != 1:
        raise ValueError("exactly one selected microgrid is required")
    selected = replace(
        case,
        microgrids=grids,
        reactive_plans={},
        joint_pq_plans={} if joint_pq is None else {region: joint_pq},
        # Cluster storage reachability uses its ordinary first-order response.
        # The unified single controller owns a different, explicitly declared
        # PCS hold model; never inherit a foreign dynamics certificate.
        storage_reachability={},
        storage_window_targets={
            k: v for k, v in case.storage_window_targets.items() if k == region
        },
        storage_reserves={
            k: v for k, v in case.storage_reserves.items() if k == region
        },
    )
    if joint_pq is not None:
        if checkpoint is None:
            raise ValueError("joint P/Q planning requires the completed checkpoint")
        ids = tuple(r.resource_id for r in joint_pq.devices.resources)
        if (
            tuple(checkpoint.active_power_mw.get(r) for r in ids)
            != joint_pq.initial.p_mw
            or tuple(checkpoint.reactive_power_mvar.get(r) for r in ids)
            != joint_pq.initial.q_mvar
            or checkpoint.storage_energy_mwh != joint_pq.initial.energy_mwh
        ):
            raise ValueError(
                "joint P/Q initial state differs from the completed checkpoint"
            )
        return selected
    return with_reactive_planning(
        selected,
        policy=policy.reactive_planning,
        dynamics=policy.dynamic_tracking,
        checkpoints={} if checkpoint is None else {region: checkpoint},
        storage_enabled=storage_enabled,
        execution_step_minutes=policy.device_step_minutes,
    )
