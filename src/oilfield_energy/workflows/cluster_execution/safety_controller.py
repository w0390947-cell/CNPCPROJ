"""One cluster commit authority, retaining a jointly certified continuation."""

# pyright: strict
from oilfield_energy.modules.control.api import (
    point_box,
    tube_contains,
    execute_cluster_device_command,
    approach_device_command,
)
from oilfield_energy.modules.control.contracts import TubeCommand
from oilfield_energy.modules.dispatch.api import observe_reserve_recovery
from oilfield_energy.modules.dispatch.contracts import ReserveRecoveryTask
from .contracts_reserve import ReserveClusterCertificate, ReserveActivationCommand
from .contracts_safety import (
    ClusterSafetyDomain,
    ClusterForecast,
    ClusterSafetyEvidence,
    ClusterSafetyStep,
    ClusterReserveEvent,
    ClusterContinuation,
)
from .safety_activation import certify_event, activation_commands
from .safety_network import forecast_at, model_at, bind_cluster_domain
from .safety_certificates import (
    construct_terminal,
    certify_continuation,
    certify_step,
    settled,
)
from .safety_reserve import certify_state_reserve


class ClusterSafetyController:
    """Proof induction does not assume the next optimizer succeeds.

    Every replacement is certified before publication. The retained command is
    used on candidate failure, without resetting state, energy or deadlines.
    """

    def __init__(self, domain: ClusterSafetyDomain):
        domain = bind_cluster_domain(domain)
        self.domain = domain
        self.minute = 0
        self.states = tuple(r.initial for r in domain.regions)
        self.terminal, self.terminal_digest = construct_terminal(domain)
        self.plan = certify_continuation(
            domain,
            tuple(point_box(s) for s in self.states),
            0,
            self.terminal,
            self.terminal_digest,
        )
        self.plans = [self.plan]
        self.steps: list[ClusterSafetyStep] = []
        self.tasks: dict[tuple[str, str], ReserveRecoveryTask] = {}
        self.task_history: list[ReserveRecoveryTask] = []
        self.observations: list[ReserveClusterCertificate] = []
        self.failure = ""
        self.events: list[ClusterReserveEvent] = []
        self.active_event: ClusterReserveEvent | None = None
        self.previous_activation: tuple[ReserveActivationCommand, ...] = ()
        self.observe_reserve()

    def certify_candidate(
        self, proposal: tuple[TubeCommand, ...]
    ) -> ClusterContinuation:
        if self.active_event is not None:
            raise ValueError("accepted reserve event owns the current control interval")
        return certify_continuation(
            self.domain,
            tuple(point_box(s) for s in self.states),
            self.minute,
            self.terminal,
            self.terminal_digest,
            proposal,
        )

    def commands_for_endpoints(
        self, targets: tuple[TubeCommand, ...]
    ) -> tuple[TubeCommand, ...]:
        """Economic targets are proposals under the same declared physical model."""
        choices: list[TubeCommand] = []
        for r, s, t in zip(self.domain.regions, self.states, targets, strict=True):
            model = model_at(r, self.minute)
            p = tuple(
                old + (value - old) / x.alpha_p
                for old, value, x in zip(s.p_mw, t.p_mw, model.resources, strict=True)
            )
            q = [
                old + (value - old) / model.alpha_q
                for old, value in zip(s.q_mvar, t.q_mvar, strict=True)
            ]
            for i, x in enumerate(model.resources):
                if x.q_over_p is not None:
                    cap = 0.98 * x.q_over_p * max(0.0, min(s.p_mw[i], p[i]))
                    q[i] = max(-cap, min(cap, q[i]))
            choices.append(
                approach_device_command(
                    model,
                    s,
                    TubeCommand(p_mw=p, q_mvar=tuple(q)),
                    next_model=model_at(r, self.minute + 1),
                )
            )
        return tuple(choices)

    def accept_reserve_event(self) -> ClusterReserveEvent:
        if not self.reserve_call_ready:
            raise ValueError(
                "new reserve call is locked until full recovery and recertification"
            )
        event = certify_event(
            self.domain,
            self.states,
            self.minute,
            self.terminal,
            self.terminal_digest,
            len(self.events) + 1,
        )
        self.events.append(event)
        self.active_event = event
        self.previous_activation = ()
        return event

    def _advance_reserve(
        self, observed: tuple[ClusterForecast, ...], requests: tuple[float, ...]
    ) -> ClusterSafetyStep:
        event = self.active_event
        assert event is not None
        command, activation = activation_commands(
            self.domain,
            event,
            self.states,
            observed,
            self.minute,
            requests,
            self.previous_activation,
        )
        proof = certify_step(
            self.domain, tuple(point_box(s) for s in self.states), command, self.minute
        )
        after = tuple(
            execute_cluster_device_command(model_at(r, self.minute), s, u)
            for r, s, u in zip(self.domain.regions, self.states, command, strict=True)
        )
        if not all(
            tube_contains(b, s)
            for b, s in zip(event.successor.initial, after, strict=True)
        ):
            raise ValueError("reserve response escaped the accepted event envelope")
        step = ClusterSafetyStep(
            minute=self.minute,
            source="reserve",
            observed=observed,
            before=self.states,
            after=after,
            commands=command,
            path=proof,
            activation=activation,
        )
        self.steps.append(step)
        self.states = after
        self.minute += 1
        self.previous_activation = activation
        if self.minute == event.end_minute:
            self.plan = event.successor
            self.plans.append(self.plan)
            self.active_event = None
        step = step.model_copy(update={"reserve": self.observe_reserve()})
        self.steps[-1] = step
        return step

    def observe_reserve(self) -> ReserveClusterCertificate | None:
        c = certify_state_reserve(
            self.domain, tuple(point_box(s) for s in self.states), self.minute
        )
        if c is None:
            return None
        self.observations.append(c)
        policy = self.domain.reserve_policy
        assert policy is not None
        for r in self.domain.regions:
            for direction in ("up", "down"):
                key = (r.region, direction)
                prior = self.tasks.get(key)
                task = observe_reserve_recovery(
                    prior,
                    region=r.region,
                    direction=direction,
                    minute=self.minute,
                    required_mw=r.required_reserve_mw,
                    recovery_minutes=policy.recovery_minutes,
                    adequate=True if c.status == "certified" else None,
                    reason=c.reason,
                )
                if task is not None:
                    self.tasks[key] = task
                    if task != prior:
                        self.task_history.append(task)
        if any(
            t.status != "recovered" and t.deadline_minute <= self.minute
            for t in self.tasks.values()
        ):
            raise ValueError(
                "original reserve recovery deadline has no actual certificate"
            )
        return c

    def validate_observation(self, observed: tuple[ClusterForecast, ...]) -> None:
        if len(observed) != len(self.domain.regions):
            raise ValueError("observed region axis differs")
        for r, o in zip(self.domain.regions, observed, strict=True):
            f = forecast_at(r, self.minute)
            if (
                len(o.load_p_mw) != len(f.load_p_mw)
                or len(o.load_q_mvar) != len(f.load_q_mvar)
                or len(o.available_mw) != len(f.available_mw)
            ):
                raise ValueError("observed resource axis differs")
            if any(
                x.low != x.high or x.low < b.low or x.high > b.high
                for x, b in zip(
                    o.load_p_mw + o.load_q_mvar,
                    f.load_p_mw + f.load_q_mvar,
                    strict=True,
                )
            ):
                raise ValueError("actual disturbance left the declared load domain")
            if any(
                x.kind in ("wind", "pv") and a < b
                for x, a, b in zip(
                    r.devices.resources, o.available_mw, f.available_mw, strict=True
                )
            ):
                raise ValueError("actual generation left the declared firm domain")

    def advance(
        self,
        observed: tuple[ClusterForecast, ...],
        proposal: tuple[TubeCommand, ...] | None,
        reason: str = "",
        *,
        reserve_requests: tuple[float, ...] | None = None,
    ) -> ClusterSafetyStep:
        self.validate_observation(observed)
        if self.active_event is not None:
            if reserve_requests is None:
                raise ValueError(
                    "accepted reserve event requires its current call signal"
                )
            return self._advance_reserve(observed, reserve_requests)
        if reserve_requests is not None:
            raise ValueError("reserve signal has no accepted event")
        offset = self.minute - self.plan.start_minute
        expected = (
            self.plan.initial
            if offset == 0
            else self.plan.steps[offset - 1].endpoints
            if offset <= len(self.plan.steps)
            else self.plan.terminal
        )
        if not all(
            tube_contains(b, s) for b, s in zip(expected, self.states, strict=True)
        ):
            raise ValueError("actual cluster state left its retained continuation")
        source = "backup" if offset < len(self.plan.commands) else "terminal"
        replacement = None
        if proposal is not None:
            try:
                replacement = self.certify_candidate(proposal)
            except ValueError as exc:
                reason = str(exc)
            else:
                offset = 0
                source = "economic"
        plan = replacement or self.plan
        command = (
            plan.commands[offset] if offset < len(plan.commands) else self.terminal
        )
        # Recompute the selected actual step. This guards corrupted/stale tails;
        # it does not rely on a fresh optimization or a fresh return search.
        proof = certify_step(
            self.domain, tuple(point_box(s) for s in self.states), command, self.minute
        )
        after = tuple(
            execute_cluster_device_command(model_at(r, self.minute), s, u)
            for r, s, u in zip(self.domain.regions, self.states, command, strict=True)
        )
        retained_end = (
            plan.steps[offset].endpoints if offset < len(plan.steps) else plan.terminal
        )
        if not all(
            tube_contains(b, s) for b, s in zip(retained_end, after, strict=True)
        ):
            raise ValueError("cluster response escaped its certified successor set")
        if replacement is not None:
            self.plan = replacement
            self.plans.append(replacement)
        before = self.states
        self.states = after
        self.minute += 1
        step = ClusterSafetyStep(
            minute=self.minute - 1,
            source=source,
            observed=observed,
            reason=reason,
            before=before,
            after=after,
            commands=command,
            path=proof,
        )
        self.steps.append(step)
        step = step.model_copy(update={"reserve": self.observe_reserve()})
        self.steps[-1] = step
        return step

    @property
    def reserve_call_ready(self) -> bool:
        """A new event needs full recovery, terminal return and fresh certification."""
        if self.active_event is not None or not settled(
            self.domain, tuple(point_box(s) for s in self.states), self.terminal
        ):
            return False
        c = certify_state_reserve(
            self.domain, tuple(point_box(s) for s in self.states), self.minute
        )
        return (
            c is not None
            and c.status == "certified"
            and all(t.status == "recovered" for t in self.tasks.values())
        )

    def evidence(self) -> ClusterSafetyEvidence:
        return ClusterSafetyEvidence(
            domain=self.domain,
            initial_continuation=self.plans[0],
            continuations=tuple(self.plans),
            steps=tuple(self.steps),
            reserve_events=tuple(self.events),
            failure=self.failure,
        )
