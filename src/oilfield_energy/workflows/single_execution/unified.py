"""Economic proposals, certified continuation, and exactly one physical executor.

The finite-horizon forecast envelope persists at its last sample. A terminal
command is proved safe for every remaining sample and the persistent tail.
Optimization success is never a premise of the safety induction.
"""

from fractions import Fraction as F
from typing import Literal

from oilfield_energy.modules.control.api import (
    approach_device_command,
    balance_policy_command,
    certify_device_transition,
    energy_return_command,
    execute_tube_command,
    point_box,
    tube_contains,
    validate_device_box,
)
from oilfield_energy.modules.control.contracts import (
    TubeCommand,
    TubeState,
)

from .admission import admit_tracking_target
from .certificates import (
    certify_unified_plan,
    certify_unified_terminal,
    device_model_at,
    unified_network_box,
    validate_unified_observation,
)
from .contracts_unified import (
    SingleForecastEnvelope,
    UnifiedDomain,
    UnifiedExecution,
    UnifiedPlanCertificate,
    UnifiedStep,
)
from .tasks import TaskCertificateUnavailable, assess_hard_task
from .tracking import certify_tracking_target
from .planning import validate_pq_proposal


class UnifiedSingleController:
    """The sole authority for committing one device cycle and retaining its tail."""

    def __init__(self, domain: UnifiedDomain, initial: TubeState) -> None:
        self.domain, self.state = domain, initial
        self.terminal_proof_digest = certify_unified_terminal(domain)
        try:
            initial_plan = certify_unified_plan(domain, point_box(initial), 0)
        except TaskCertificateUnavailable:
            initial_plan = certify_unified_plan(
                domain, point_box(initial), 0, require_task=False
            )
        self.plans = [initial_plan]
        self.task_required = initial_plan.task_continuation is not None
        self.steps: list[UnifiedStep] = []
        self.target: tuple[float, float] | None = None
        self.deadline = domain.response_steps
        self.expected = point_box(initial)
        self.feedback_disposition: Literal["economic", "retained_feedback"] = "economic"

    def advance(
        self,
        observed: SingleForecastEnvelope,
        *,
        context_id: str,
        target: tuple[float, float],
        proposal: TubeCommand | None,
        proposal_failure: str = "",
        target_changed: bool = False,
        target_plan: UnifiedPlanCertificate | None = None,
        target_retained: bool = False,
    ) -> UnifiedStep:
        k, domain = len(self.steps), self.domain
        if context_id != domain.context_id:
            raise ValueError(
                "network, authorization, dynamics or forecast context changed"
            )
        validate_unified_observation(domain, k, observed)
        if not tube_contains(self.expected, self.state):
            raise ValueError("actual state left the retained certified continuation")
        plan, reason = self.plans[-1], proposal_failure
        offset = k - plan.start_step
        disposition = "backup" if offset < len(plan.commands) else "terminal"
        replacement: UnifiedPlanCertificate | None = None
        feedback_disposition = self.feedback_disposition
        accepted_fraction = 0.0
        if target_plan is not None:
            previous_tracking = self.plans[-1].tracking
            if (
                previous_tracking is not None
                and k < previous_tracking.guaranteed_until_step
                and target != self.target
            ):
                raise ValueError(
                    "an active accepted tracking promise cannot be replaced before its expiry"
                )
            if target_retained and (self.target is None or target != self.target):
                raise ValueError(
                    "retained target renewal cannot change the accepted target"
                )
            tracking = target_plan.tracking
            if (
                tracking is None
                or target_plan.start_step != k
                or target_plan.initial != point_box(self.state)
            ):
                raise ValueError(
                    "target admission does not inherit the completed actual state"
                )
            if target != (tracking.target_p_mw, tracking.target_q_mvar):
                raise ValueError(
                    "target differs from its admitted tracking certificate"
                )
            expected_deadline = (
                self.deadline if target == self.target else k + domain.response_steps
            )
            if tracking.deadline_step != expected_deadline:
                raise ValueError(
                    "target admission changed the original response deadline"
                )
            recomputed = certify_tracking_target(
                domain,
                point_box(self.state),
                k,
                tracking.guaranteed_until_step,
                tracking.deadline_step,
                target,
                tracking.nominal,
                require_task=self.task_required,
            )
            if recomputed != target_plan:
                raise ValueError("target admission certificate does not recompute")
            plan, offset, disposition = target_plan, 0, "economic"
            replacement, accepted_fraction = target_plan, 1.0
            feedback_disposition = (
                "retained_feedback" if target_retained else "economic"
            )
        tracking = plan.tracking
        feedback_policy = tracking is not None and offset < len(tracking.policies)
        if proposal is not None and not feedback_policy:
            backup = (
                plan.commands[offset]
                if offset < len(plan.commands)
                else domain.terminal_command
            )
            candidates = [proposal]
            try:
                model = device_model_at(domain, k)
                # Reject malformed/out-of-capability commands. For admissible
                # setpoints repair only response/next-step constraints locally.
                validate_device_box(
                    model,
                    point_box(
                        self.state.model_copy(
                            update={
                                "p_mw": proposal.p_mw,
                                "q_mvar": proposal.q_mvar,
                            }
                        )
                    ),
                )
                local = approach_device_command(
                    model,
                    self.state,
                    proposal,
                    next_model=device_model_at(domain, k + 1),
                )
                if local != proposal:
                    candidates.append(local)
                # Device-block alternatives retain unrelated commands. Each
                # receives the full tail proof; no partial acceptance is used.
                for i in range(len(model.resources)):
                    candidates.append(
                        TubeCommand(
                            p_mw=tuple(
                                backup.p_mw[j] if j == i else p
                                for j, p in enumerate(local.p_mw)
                            ),
                            q_mvar=tuple(
                                backup.q_mvar[j] if j == i else q
                                for j, q in enumerate(local.q_mvar)
                            ),
                        )
                    )
            except ValueError:
                pass
            for candidate in candidates:
                try:
                    replacement = certify_unified_plan(
                        domain,
                        point_box(self.state),
                        k,
                        (candidate,),
                        require_task=self.task_required,
                    )
                except ValueError as exc:
                    reason = str(exc)
                else:
                    accepted_fraction = 1.0 if candidate == proposal else 0.0
                    if candidate != proposal:
                        reason = (
                            "candidate repaired by certified local device allocation"
                        )
                    plan, offset, disposition = replacement, 0, "economic"
                    break
        deadline = self.deadline
        if self.target is None or (target_changed and target != self.target):
            deadline = k + domain.response_steps
        command = (
            plan.commands[offset]
            if offset < len(plan.commands)
            else domain.terminal_command
        )
        if feedback_policy:
            assert tracking is not None
            command = balance_policy_command(
                device_model_at(domain, k),
                self.state,
                tracking.policies[offset],
                float(sum((F(b.p_mw.low) for b in observed.loads), F(0))),
                float(sum((F(b.q_mvar.low) for b in observed.loads), F(0))),
            )
            disposition, accepted_fraction = feedback_disposition, 1.0
        recovery = plan.task_continuation
        recovery_offset = k - recovery.recovery_start_step if recovery else -1
        energy_feedback = recovery is not None and 0 <= recovery_offset < len(
            recovery.policies
        )
        if energy_feedback:
            assert recovery is not None
            command = energy_return_command(
                device_model_at(domain, k),
                self.state,
                recovery.policies[recovery_offset],
            )
        # Recompute the retained prefix against the observed state before commit.
        source = (
            point_box(self.state)
            if feedback_policy or energy_feedback
            else plan.initial
            if replacement is not None
            else self.expected
        )
        transition = certify_device_transition(
            device_model_at(domain, k), source, command
        )
        network = unified_network_box(domain, transition.path, observed)
        if not network.certified:
            raise ValueError(
                "observed executed path fails its AC certificate: " + network.reason
            )
        after = execute_tube_command(device_model_at(domain, k), self.state, command)
        if not tube_contains(transition.endpoint, after):
            raise ValueError("executed response escaped its certified enclosure")
        if feedback_policy:
            assert tracking is not None
            if not tube_contains(tracking.tubes[offset + 1], after):
                raise ValueError("executed feedback escaped its target tracking tube")
        if energy_feedback:
            assert recovery is not None
            if not tube_contains(recovery.tubes[recovery_offset + 1], after):
                raise ValueError("executed energy feedback escaped its task tube")
        endpoint = unified_network_box(domain, point_box(after), observed)
        p, q = endpoint.pcc_p_mw, endpoint.pcc_q_mvar
        if not endpoint.certified or p is None or q is None:
            raise ValueError("executed endpoint has no AC enclosure")
        track = (
            max(abs(p.low - target[0]), abs(p.high - target[0])) <= domain.p_band_mw
            and max(abs(q.low - target[1]), abs(q.high - target[1]))
            <= domain.q_band_mvar
        )
        row = UnifiedStep(
            step=k,
            before=self.state,
            after=after,
            command=command,
            proposed_command=proposal,
            candidate_fraction=accepted_fraction,
            observed=observed,
            disposition=disposition,
            proposal_reason=reason,
            plan_index=len(self.plans) if replacement else len(self.plans) - 1,
            plan_offset=offset,
            requested_p_mw=target[0],
            requested_q_mvar=target[1],
            deadline_step=deadline,
            pcc_p_mw=p,
            pcc_q_mvar=q,
            tracking_certified=track,
        )
        if replacement is not None:
            self.plans.append(replacement)
            self.feedback_disposition = feedback_disposition
            self.task_required = (
                self.task_required or replacement.task_continuation is not None
            )
        self.steps.append(row)
        self.state = after
        if feedback_policy:
            assert tracking is not None
            self.expected = tracking.tubes[offset + 1]
        elif energy_feedback:
            assert recovery is not None
            self.expected = recovery.tubes[recovery_offset + 1]
        else:
            self.expected = transition.endpoint
        self.deadline, self.target = deadline, target
        return row


def verify_unified_execution(evidence: UnifiedExecution) -> None:
    """Recompute mathematical certificates and execution; reject altered claims."""
    certify_tracking_target.cache_clear()
    UnifiedExecution.model_validate(evidence.model_dump())
    for proposal in evidence.economic_proposals:
        validate_pq_proposal(evidence.domain, proposal)
    if not evidence.plans:
        if evidence.status == "passed" or evidence.steps:
            raise ValueError("executed evidence has no initial certified continuation")
        return
    controller = UnifiedSingleController(evidence.domain, evidence.initial)
    if (
        controller.terminal_proof_digest != evidence.terminal_proof_digest
        or controller.plans[0] != evidence.plans[0]
    ):
        raise ValueError("initial or terminal certificate does not recompute")
    admissions = {a.start_step: a for a in evidence.target_admissions}
    if len(admissions) != len(evidence.target_admissions):
        raise ValueError("duplicate target admission")
    proposals = {p.start_step: p for p in evidence.economic_proposals}
    for row in evidence.steps:
        if row.plan_index not in (len(controller.plans), len(controller.plans) - 1):
            raise ValueError("certificate adoption order differs")
        target = (row.requested_p_mw, row.requested_q_mvar)
        plan = None
        retained = False
        if row.step in admissions:
            admission = admissions.pop(row.step)
            proposal = proposals.get(row.step)
            retained = admission.origin == "retained"
            if retained:
                old = controller.plans[-1].tracking
                if (
                    old is None
                    or controller.target is None
                    or (admission.requested_p_mw, admission.requested_q_mvar)
                    != controller.target
                    or admission.nominal != old.nominal
                    or admission.end_step
                    != min(row.step + evidence.update_steps, evidence.requested_steps)
                    or (
                        proposal is not None
                        and proposal.command is not None
                        and proposal.result.status == "passed"
                    )
                ):
                    raise ValueError(
                        "retained renewal differs from the accepted target or solver failure"
                    )
                requested = controller.target
                nominal = old.nominal
            elif (
                proposal is None
                or proposal.command is None
                or (
                    admission.end_step
                    != min(row.step + evidence.update_steps, evidence.requested_steps)
                    or admission.origin != proposal.target_origin
                    or admission.nominal != proposal.command
                    or (admission.requested_p_mw, admission.requested_q_mvar)
                    != (proposal.target_p_mw, proposal.target_q_mvar)
                )
            ):
                raise ValueError(
                    "target admission differs from its economic/external request"
                )
            else:
                assert proposal is not None and proposal.command is not None
                requested = (proposal.target_p_mw, proposal.target_q_mvar)
                nominal = proposal.command
            expected = admit_tracking_target(
                evidence.domain,
                controller.state,
                row.step,
                admission.end_step,
                requested,
                nominal,
                admission.origin,
                controller.target,
                controller.deadline,
                require_task=controller.task_required,
            )
            if expected != admission:
                raise ValueError("target admission does not recompute")
            plan = admission.plan
        replay = controller.advance(
            row.observed,
            context_id=evidence.domain.context_id,
            target=target,
            proposal=row.proposed_command,
            proposal_failure=row.proposal_reason,
            target_changed=controller.target != target,
            target_plan=plan,
            target_retained=retained,
        )
        if replay != row or controller.plans != list(
            evidence.plans[: len(controller.plans)]
        ):
            raise ValueError(
                "recorded command, response, target deadline or AC evidence does not recompute"
            )
    if controller.plans != list(evidence.plans):
        raise ValueError("unexecuted certificate recorded as adopted")
    if admissions:
        raise ValueError("unexecuted target admission recorded")
    if evidence.hard_task != assess_hard_task(
        evidence.domain, evidence.plans, evidence.steps, evidence.status
    ):
        raise ValueError(
            "hard task assessment differs from the executed certificate chain"
        )
    UnifiedExecution.model_validate(evidence.model_dump())
