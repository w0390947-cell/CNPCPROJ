"""The single production orchestration; numerical and observation ports are injected."""

from collections.abc import Callable
from typing import Literal

from oilfield_energy.modules.control.contracts import TubeState

from .admission import admit_tracking_target
from .planning import validate_pq_proposal
from .contracts_unified import (
    SingleEconomicProposal,
    SingleForecastEnvelope,
    TargetAdmission,
    UnifiedDomain,
    UnifiedExecution,
)
from .tasks import assess_hard_task
from .unified import UnifiedSingleController


def run_unified_feedback(
    domain: UnifiedDomain,
    initial: TubeState,
    *,
    total_steps: int,
    update_steps: int,
    initial_target: tuple[float, float],
    observe: Callable[[int], SingleForecastEnvelope],
    propose: Callable[[int, TubeState], SingleEconomicProposal],
    progress: Callable[[int], None] | None = None,
    live: Callable[[dict], None] | None = None,
) -> UnifiedExecution:
    controller: UnifiedSingleController | None = None
    proposals: list[SingleEconomicProposal] = []
    admissions: list[TargetAdmission] = []
    target = initial_target
    reason, failure = "", ""
    status: Literal["passed", "unproven", "assumption_violated"] = "unproven"
    if live:
        live({"kind": "initial", "total": total_steps, "initial": initial.model_dump(mode="json"),
              "domain": domain.model_dump(mode="json"), "update_steps": update_steps,
              "devices": domain.devices.model_dump(mode="json"), "network": domain.network.model_dump(mode="json"),
              "energy_task": domain.energy_task.model_dump(mode="json") if domain.energy_task else None,
              "p_band_mw": domain.p_band_mw, "q_band_mvar": domain.q_band_mvar})
    try:
        controller = UnifiedSingleController(domain, initial)
        for k in range(total_steps):
            observed = observe(k)
            changed = False
            target_plan = None
            target_retained = False
            admission = None
            if k % update_steps == 0:
                if live:
                    live({"kind": "phase", "minute": k, "message": "滚动经济规划与目标接纳"})
                failure = ""
                try:
                    proposal = propose(k, controller.state)
                    if proposal.start_step != k or proposal.initial != controller.state:
                        raise ValueError(
                            "economic planner did not use the completed actual state"
                        )
                    validate_pq_proposal(domain, proposal)
                    proposals.append(proposal)
                    if (
                        proposal.result.status == "passed"
                        and proposal.command is not None
                    ):
                        admission = admit_tracking_target(
                            domain,
                            controller.state,
                            k,
                            min(k + update_steps, total_steps),
                            (proposal.target_p_mw, proposal.target_q_mvar),
                            proposal.command,
                            proposal.target_origin,
                            controller.target,
                            controller.deadline,
                            require_task=controller.task_required,
                        )
                        admissions.append(admission)
                        if admission.plan is not None:
                            target_plan = admission.plan
                            t = target_plan.tracking
                            assert t is not None
                            new_target = (t.target_p_mw, t.target_q_mvar)
                            changed, target = new_target != target, new_target
                        else:
                            failure = "target not certified: " + admission.reason
                    else:
                        failure = (
                            proposal.result.reason or "economic proposal unavailable"
                        )
                except (ValueError, RuntimeError, FloatingPointError) as exc:
                    failure = str(exc)
                # An unavailable economic solve must not revoke a trackable
                # accepted target. Certify a new finite window for that exact
                # target and its original deadline, using the same policy law.
                old_tracking = controller.plans[-1].tracking
                if (
                    admission is None
                    and controller.target is not None
                    and old_tracking is not None
                ):
                    renewal = admit_tracking_target(
                        domain,
                        controller.state,
                        k,
                        min(k + update_steps, total_steps),
                        controller.target,
                        old_tracking.nominal,
                        "retained",
                        controller.target,
                        controller.deadline,
                        require_task=controller.task_required,
                    )
                    admissions.append(renewal)
                    if renewal.plan is not None:
                        target_plan, target_retained = renewal.plan, True
                    else:
                        failure += (
                            "; retained target renewal not certified: " + renewal.reason
                        )
            command = None
            cycle_failure = failure
            row = controller.advance(
                observed,
                context_id=domain.context_id,
                target=target,
                proposal=command,
                proposal_failure=cycle_failure,
                target_changed=changed,
                target_plan=target_plan,
                target_retained=target_retained,
            )
            if live:
                tracking = controller.plans[row.plan_index].tracking
                cycle = row.cycle_evidence
                live({"kind": "cycle", "minute": k + 1,
                      "row": row.model_dump(mode="json", exclude={"cycle_evidence"}),
                      "voltage": ([v.model_dump(mode="json") for v in cycle.endpoint_network.voltage_bounds] if cycle and cycle.endpoint_network else None),
                      "network_certified": cycle.endpoint_network.certified if cycle and cycle.endpoint_network else None,
                      "tracking_until": tracking.guaranteed_until_step if tracking else None,
                      "message": "设备执行与反馈"})
            if progress is not None:
                progress(k + 1)
        status = "passed"
    except (ValueError, RuntimeError, FloatingPointError) as exc:
        reason = str(exc)
        status = "assumption_violated" if controller is not None else "unproven"
    rows = tuple(controller.steps) if controller else ()
    assessed = tuple(r for r in rows if r.step + 1 >= r.deadline_step)
    tracking: Literal["passed", "violated", "not_assessed"] = "not_assessed"
    if assessed:
        tracking = (
            "passed" if all(r.tracking_certified for r in assessed) else "violated"
        )
    return UnifiedExecution(
        domain=domain,
        initial=initial,
        status=status,
        reason=reason,
        requested_steps=total_steps,
        update_steps=update_steps,
        steps=rows,
        plans=tuple(controller.plans) if controller else (),
        terminal_proof_digest=controller.terminal_proof_digest if controller else "",
        tracking_status=tracking,
        economic_proposals=tuple(proposals),
        target_admissions=tuple(admissions),
        target_admission_status="not_assessed"
        if not admissions
        else "passed"
        if all(a.accepted for a in admissions)
        else "rejected",
        hard_task=assess_hard_task(
            domain, tuple(controller.plans) if controller else (), rows, status
        ),
    )
