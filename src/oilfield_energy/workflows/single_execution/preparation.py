"""Pure preparation of one certified device cycle; no state is committed."""

from fractions import Fraction as F

from oilfield_energy.modules.control.api import (
    balance_policy_command,
    certify_device_transition,
    energy_return_command,
    execute_tube_command,
    point_box,
    tube_contains,
)
from oilfield_energy.modules.control.contracts import TubeBox, TubeState
from .certificates import (
    device_model_at,
    unified_network_box,
    validate_unified_observation,
)
from .contracts_unified import (
    UnifiedDomain,
    UnifiedPlanCertificate,
    SingleForecastEnvelope,
)
from .contracts_preparation import PreparedUnifiedCycle


def prepare_certified_cycle(
    domain: UnifiedDomain,
    state: TubeState,
    expected: TubeBox,
    step: int,
    plan: UnifiedPlanCertificate,
    observed: SingleForecastEnvelope,
) -> PreparedUnifiedCycle:
    """Calculate a modeled response using the retained proof, without execution.

    The caller owns admission of the plan, terminal proof and current context.
    This operation rechecks one observed transition; it does not independently
    admit a new plan or establish field execution/communication guarantees.
    """
    k = step
    offset = k - plan.start_step
    if offset < 0 or not tube_contains(expected, state):
        raise ValueError("State or step lies outside the retained continuation")
    validate_unified_observation(domain, k, observed)
    tracking = plan.tracking
    feedback_policy = tracking is not None and offset < len(tracking.policies)
    command = (
        plan.commands[offset]
        if offset < len(plan.commands)
        else domain.terminal_command
    )
    if feedback_policy:
        assert tracking is not None
        command = balance_policy_command(
            device_model_at(domain, k),
            state,
            tracking.policies[offset],
            float(sum((F(b.p_mw.low) for b in observed.loads), F(0))),
            float(sum((F(b.q_mvar.low) for b in observed.loads), F(0))),
        )
    recovery = plan.task_continuation
    recovery_offset = k - recovery.recovery_start_step if recovery else -1
    energy_feedback = recovery is not None and 0 <= recovery_offset < len(
        recovery.policies
    )
    if energy_feedback:
        assert recovery is not None
        command = energy_return_command(
            device_model_at(domain, k),
            state,
            recovery.policies[recovery_offset],
        )
    # Recompute the retained prefix against the observed state before commit.
    source = point_box(state) if feedback_policy or energy_feedback else expected
    transition = certify_device_transition(device_model_at(domain, k), source, command)
    network = unified_network_box(domain, transition.path, observed)
    if not network.certified:
        raise ValueError(
            "observed predicted path fails its AC certificate: " + network.reason
        )
    after = execute_tube_command(device_model_at(domain, k), state, command)
    if not tube_contains(transition.endpoint, after):
        raise ValueError("predicted response escaped its certified enclosure")
    if feedback_policy:
        assert tracking is not None
        if not tube_contains(tracking.tubes[offset + 1], after):
            raise ValueError("predicted feedback escaped its target tracking tube")
    if energy_feedback:
        assert recovery is not None
        if not tube_contains(recovery.tubes[recovery_offset + 1], after):
            raise ValueError("predicted energy feedback escaped its task tube")
    endpoint = unified_network_box(domain, point_box(after), observed)
    p, q = endpoint.pcc_p_mw, endpoint.pcc_q_mvar
    if not endpoint.certified or p is None or q is None:
        raise ValueError("predicted endpoint has no AC enclosure")
    expected_next = (
        tracking.tubes[offset + 1]
        if feedback_policy and tracking is not None
        else recovery.tubes[recovery_offset + 1]
        if energy_feedback and recovery is not None
        else transition.endpoint
    )
    return PreparedUnifiedCycle(
        command=command,
        predicted_endpoint=after,
        transition=transition,
        path_network=network,
        endpoint_network=endpoint,
        expected_next=expected_next,
        feedback_policy=feedback_policy,
        energy_feedback=energy_feedback,
    )
