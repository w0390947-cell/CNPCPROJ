"""Same-window pool ownership and provenance; selection belongs to dispatch."""

from oilfield_energy.modules.dispatch.api import (
    equivalent_coordination_calibration,
    select_network_candidates,
)
from oilfield_energy.modules.dispatch.contracts import (
    CoordinationRecoveryPolicy,
    NetworkCandidatePolicy,
    NetworkCandidateSelection,
    PCCTrackingLimits,
    RegionalNetworkCandidate,
)
from oilfield_energy.modules.power_flow.contracts import ClusterPowerFactorPolicy

from .contracts import CoordinationRecoveryRound, RegionalCalibrationFeedback


class NetworkCandidatePool:
    """One recovery invocation only; no cache, state advance or plant access."""

    def __init__(
        self,
        *,
        names: tuple[str, ...],
        time_minutes: tuple[float, ...],
        policy: NetworkCandidatePolicy,
        recovery: CoordinationRecoveryPolicy,
        tracking: PCCTrackingLimits,
        power_factor: ClusterPowerFactorPolicy,
        import_limit_mw: float,
    ) -> None:
        self.names, self.time_minutes = names, time_minutes
        self.policy, self.recovery = policy, recovery
        self.tracking, self.power_factor = tracking, power_factor
        self.import_limit_mw = import_limit_mw
        self.candidates: list[RegionalNetworkCandidate] = []
        self.attempted: set[tuple[tuple[str, int], ...]] = set()
        self.pending: tuple[RegionalNetworkCandidate, ...] = ()

    def take_pending(self) -> tuple[RegionalNetworkCandidate, ...]:
        """Consume a joint selection exactly once before the next strict trial."""
        result = self.pending
        self.pending = ()
        return result

    def add(self, feedback: tuple[RegionalCalibrationFeedback, ...]) -> None:
        for item in feedback:
            row = item.evidence
            if item.calibration is None or row.status != "passed":
                raise ValueError("network candidate requires certified diagnostic feedback")
            candidate = RegionalNetworkCandidate(
                region=row.region,
                source_round=item.reference_round,
                time_minutes=row.time_minutes,
                p_mw=row.p_mw,
                q_mvar=row.q_mvar,
                calibration=item.calibration,
            )
            if any(
                old.region == candidate.region
                and all(
                    abs(a - b) <= self.recovery.calibration_tolerance_mw
                    for a, b in zip(old.p_mw, candidate.p_mw)
                )
                and all(
                    abs(a - b) <= self.recovery.calibration_tolerance_mvar
                    for a, b in zip(old.q_mvar, candidate.q_mvar)
                )
                and equivalent_coordination_calibration(
                    old.calibration, candidate.calibration, self.recovery
                )
                for old in self.candidates
            ):
                continue
            self.candidates.append(candidate)

    def select(
        self,
        record: CoordinationRecoveryRound,
        margin: tuple[float, ...],
    ) -> NetworkCandidateSelection:
        result = select_network_candidates(
            candidates=tuple(self.candidates),
            names=self.names,
            time_minutes=self.time_minutes,
            p_reference_mw=record.p_target_mw,
            q_reference_mvar=record.q_target_mvar,
            import_limit_mw=self.import_limit_mw,
            import_tolerance_mw=self.tracking.numerical_tolerance,
            power_factor_target=self.power_factor.target,
            power_factor_tolerance=self.power_factor.numerical_tolerance,
            power_factor_margin_mvar=margin,
            tracking=self.tracking,
            policy=self.policy,
            excluded=frozenset(self.attempted),
        )
        if result.candidates:
            self.attempted.add(tuple((c.region, c.source_round) for c in result.candidates))
        self.pending = result.candidates
        return result
