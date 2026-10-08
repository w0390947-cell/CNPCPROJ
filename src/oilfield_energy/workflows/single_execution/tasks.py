"""Construct a safe feedback continuation to an absolute energy deadline."""

from fractions import Fraction as F
from hashlib import sha256

from oilfield_energy.modules.control.api import (
    certify_device_transition,
    certify_energy_return,
    validate_device_box,
)
from oilfield_energy.modules.control.contracts import EnergyReturnPolicy, TubeBox

from .contracts_tasks import HardTaskAssessment, TaskContinuation
from .contracts_unified import UnifiedDomain, UnifiedPlanCertificate, UnifiedStep
from .network import device_model_at, forecast_at, unified_network_box


class TaskCertificateUnavailable(ValueError):
    """Constructive joint proof unavailable; not a general infeasibility verdict."""


def _inside(domain: UnifiedDomain, box: TubeBox) -> bool:
    task = domain.energy_task
    assert task is not None
    return (
        task.energy_mwh.low <= box.energy_mwh.low
        and box.energy_mwh.high <= task.energy_mwh.high
    )


def complete_energy_task(
    domain: UnifiedDomain, plan: UnifiedPlanCertificate
) -> UnifiedPlanCertificate:
    if domain.energy_task is None:
        return plan
    try:
        return _complete(domain, plan)
    except ValueError as exc:
        raise TaskCertificateUnavailable(
            "hard task joint certificate unavailable: " + str(exc)
        ) from exc


def _complete(
    domain: UnifiedDomain, plan: UnifiedPlanCertificate
) -> UnifiedPlanCertificate:
    task = domain.energy_task
    assert task is not None
    storage = next(r for r in domain.devices.resources if r.kind == "storage")
    if plan.start_step > task.deadline_step:
        raise ValueError("a new certificate cannot prove a past task deadline")
    remaining = (
        F(max(0, task.deadline_step - plan.start_step))
        * F(domain.devices.step_minutes)
        / 60
    )
    energy = plan.initial.energy_mwh
    if F(energy.high) - F(storage.p.high) * remaining / F(
        domain.devices.eta_discharge
    ) > F(task.energy_mwh.high) or F(energy.low) - F(storage.p.low) * remaining * F(
        domain.devices.eta_charge
    ) < F(task.energy_mwh.low):
        raise ValueError("necessary energy reachability bound excludes the source tube")
    box = plan.initial
    at_deadline = box.energy_mwh if plan.start_step == task.deadline_step else None
    for offset, command in enumerate(plan.commands):
        box = certify_device_transition(
            device_model_at(domain, plan.start_step + offset), box, command
        ).endpoint
        if plan.start_step + offset + 1 == task.deadline_step:
            at_deadline = box.energy_mwh
    if at_deadline is not None and not (
        task.energy_mwh.low <= at_deadline.low
        and at_deadline.high <= task.energy_mwh.high
    ):
        raise ValueError("safe return does not meet the original energy deadline")
    start = plan.start_step + len(plan.commands)
    policies: list[EnergyReturnPolicy] = []
    tubes = [box]
    commands = list(plan.commands)
    digest = sha256(plan.network_proof_digest.encode())
    while at_deadline is None and not _inside(domain, box):
        k = start + len(policies)
        if k >= task.deadline_step:
            raise ValueError(
                "constructed continuation exhausted the remaining task time"
            )
        # Each direction <= half the ramp permits every consecutive feedback
        # extreme, including direction reversals. Full capabilities are checked.
        failure = "no energy-return movement certified"
        for fraction in (1.0, 0.5, 0.25, 0.125, 0.0625):
            policy = EnergyReturnPolicy(
                base=domain.terminal_command,
                target_mwh=(task.energy_mwh.low + task.energy_mwh.high) / 2,
                charge_limit_mw=min(-storage.p.low, storage.ramp_p_mw / 2) * fraction,
                discharge_limit_mw=min(storage.p.high, storage.ramp_p_mw / 2)
                * fraction,
            )
            try:
                transition = certify_energy_return(
                    device_model_at(domain, k), box, policy
                )
                validate_device_box(device_model_at(domain, k + 1), transition.endpoint)
                network = unified_network_box(
                    domain, transition.path, forecast_at(domain, k), reusable=True
                )
                if not network.certified:
                    raise ValueError(network.reason)
                if transition.endpoint.energy_mwh == box.energy_mwh:
                    raise ValueError("no certified energy progress")
            except ValueError as exc:
                failure = str(exc)
            else:
                break
        else:
            raise ValueError(failure)
        policies.append(policy)
        commands.append(policy.base)
        box = transition.endpoint
        tubes.append(box)
        digest.update(network.model_dump_json().encode())
    if at_deadline is None:
        at_deadline = box.energy_mwh
    if policies:
        # Switch off PCS power before entering the already certified invariant
        # terminal set. This changes no energy and may follow the task deadline.
        k = start + len(policies)
        transition = certify_device_transition(
            device_model_at(domain, k), box, domain.terminal_command
        )
        network = unified_network_box(
            domain, transition.path, forecast_at(domain, k), reusable=True
        )
        if not network.certified:
            raise ValueError(network.reason)
        commands.append(domain.terminal_command)
        box = transition.endpoint
        digest.update(network.model_dump_json().encode())
    continuation = TaskContinuation(
        task=task,
        recovery_start_step=start,
        policies=tuple(policies),
        tubes=tuple(tubes),
        deadline_energy_mwh=at_deadline,
    )
    return plan.model_copy(
        update={
            "commands": tuple(commands),
            "terminal": box,
            "network_proof_digest": digest.hexdigest(),
            "task_continuation": continuation,
        }
    )


def assess_hard_task(
    domain: UnifiedDomain,
    plans: tuple[UnifiedPlanCertificate, ...],
    steps: tuple[UnifiedStep, ...],
    status: str,
) -> HardTaskAssessment:
    task = domain.energy_task
    if task is None:
        return HardTaskAssessment(guarantee="not_required", outcome="not_required")
    initial = bool(plans and plans[0].task_continuation is not None)
    maintained = initial and all(p.task_continuation is not None for p in plans)
    outcome = "pending"
    if len(steps) >= task.deadline_step:
        value = steps[task.deadline_step - 1].after.energy_mwh
        outcome = (
            "met" if task.energy_mwh.low <= value <= task.energy_mwh.high else "missed"
        )
    return HardTaskAssessment(
        guarantee="assumption_violated"
        if status == "assumption_violated"
        else "maintained"
        if maintained
        else "unproven",
        outcome=outcome,
        initial_certified=initial,
        reason=""
        if maintained
        else (
            plans[0].task_reason
            if plans
            else "Initial joint task certificate was not obtained; safety-only execution does not guarantee the hard task."
        ),
    )
