"""Actual-injection minute AC feedback and latched recovery interlock.

No optimizer, state estimation guess, or last-good-as-current fallback is used.
The evaluator accepts normalized snapshots independently of the SIL simulator.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime
from math import isfinite
from typing import Mapping

from .actuation_arbiter import ActuationArbiter
from .control_contracts import CapIntentAction, SignalValidity, StationCapIntent
from .network_model import NetworkModelV2, assess_network_model
from .network_scenarios import NetworkSecurityLimits, PointFlowResult, ScenarioStatus, evaluate_operating_point
from .power_flow_comparison import (
    FixedStateSnapshot,
    build_operating_point,
    device_capability_violations,
)


@dataclass(frozen=True)
class MinuteNetworkConfig:
    max_age_seconds: float = 60.0
    require_field_approval: bool = False
    safe_confirmation_cycles: int = 2

    def __post_init__(self):
        if isinstance(self.max_age_seconds, bool) or not isfinite(self.max_age_seconds) or self.max_age_seconds < 0:
            raise ValueError("max_age_seconds must be finite and nonnegative")
        if type(self.safe_confirmation_cycles) is not int or self.safe_confirmation_cycles < 2:
            raise ValueError("at least two distinct safe cycles are required")
        if type(self.require_field_approval) is not bool:
            raise ValueError("require_field_approval must be boolean")


@dataclass(frozen=True)
class MinuteNetworkFeedback:
    evaluated_at: datetime
    observed_at: datetime | None
    snapshot_id: str
    operating_mode_id: str
    status: str
    reasons: tuple[str, ...]
    valid: bool
    voltage_within_limits: bool | None = None
    line_capacity_within_limits: bool | None = None
    flow: PointFlowResult | None = None
    inputs: dict | None = None
    device_capacity_within_limits: bool | None = None
    device_violations: tuple[dict, ...] = ()

    @property
    def recovery_safe(self) -> bool:
        return (
            self.valid
            and self.voltage_within_limits is True
            and self.line_capacity_within_limits is True
            and self.device_capacity_within_limits is not False
        )

    @property
    def validity(self) -> SignalValidity:
        return SignalValidity.valid_signal() if self.valid else SignalValidity.invalid_signal()


class MinuteNetworkEvaluator:
    def __init__(self, network: NetworkModelV2 | None, limits: NetworkSecurityLimits,
                 device_buses: Mapping[str, str], *, config: MinuteNetworkConfig | None = None):
        self.network = network
        self.limits = limits
        self.device_buses = dict(device_buses)
        self.config = config or MinuteNetworkConfig()

    def evaluate(self, snapshot: FixedStateSnapshot, *, at: datetime,
                 operating_mode_id: str) -> MinuteNetworkFeedback:
        if at.utcoffset() is None:
            raise ValueError("evaluation time must include a timezone")

        def failed(status, *reasons):
            return MinuteNetworkFeedback(at, snapshot.at, snapshot.snapshot_id,
                                         operating_mode_id, status, tuple(reasons), False,
                                         inputs=dict(snapshot=asdict(snapshot)))

        age = (at - snapshot.at).total_seconds()
        if age < 0:
            return failed("invalid_input", "NETWORK_SNAPSHOT_FROM_FUTURE")
        if age > self.config.max_age_seconds:
            return failed("invalid_input", "NETWORK_SNAPSHOT_STALE")
        if not snapshot.quality_valid or not snapshot.coherent:
            return failed("invalid_input", "NETWORK_SNAPSHOT_QUALITY_OR_COHERENCE_INVALID")
        if self.network is None:
            return failed("invalid_input", "NETWORK_MODEL_MISSING")
        if {d.resource_id: d.bus_id for d in snapshot.devices} != self.device_buses:
            return failed("invalid_input", "NETWORK_DEVICE_MAPPING_MISMATCH")
        readiness = assess_network_model(self.network, operating_mode_id,
                                         require_field_approval=self.config.require_field_approval)
        if not readiness.structurally_valid:
            return failed("invalid_input", *readiness.blockers)
        if readiness.disconnected_bus_ids:
            return failed("islanded", *readiness.blockers)
        if not readiness.ready_for_current_solver:
            return failed("unsupported", *readiness.blockers)
        if self.config.require_field_approval and not readiness.ready_for_field_case:
            return failed("invalid_input", *readiness.blockers)
        try:
            point = build_operating_point(snapshot, self.network)
            flow = evaluate_operating_point(readiness.require_current_solver_ready(), point, self.limits,
                                            slack_voltage_pu=snapshot.slack_voltage_pu)
        except (ValueError, OverflowError, FloatingPointError) as exc:
            return failed("invalid_input", str(exc))
        valid = flow.status in (ScenarioStatus.SECURE, ScenarioStatus.VIOLATION)
        codes = {v.code for v in flow.violations}
        device_violations = tuple(
            device_capability_violations(snapshot, self.limits.comparison_tolerance)
        )
        return MinuteNetworkFeedback(
            at,
            snapshot.at,
            snapshot.snapshot_id,
            operating_mode_id,
            "violation" if valid and device_violations else flow.status.value,
            flow.reasons,
            valid,
            not bool(codes & {"VOLTAGE_LOW", "VOLTAGE_HIGH"}) if valid else None,
            "BRANCH_OVERLOAD" not in codes if valid else None,
            flow,
            dict(
                snapshot=asdict(snapshot),
                p_demand_mw_by_bus=dict(point.p_demand_mw_by_bus),
                q_demand_mvar_by_bus=dict(point.q_demand_mvar_by_bus),
                limits=asdict(self.limits),
                network_id=self.network.network_id,
                provenance=asdict(self.network.provenance),
            ),
            device_capacity_within_limits=not bool(device_violations) if valid else None,
            device_violations=device_violations,
        )


class NetworkRecoveryInterlock:
    """Freeze achieved PV output, preserving all other owners' stricter caps.

Only explicit rearm after distinct safe observations transfers frozen limits to
group_control. Transfer is limit-preserving: it cannot itself increase output.
"""
    owner = "network_safety"

    def __init__(self, confirmation_cycles: int = 2, max_gap_seconds: float = 60.0):
        if type(confirmation_cycles) is not int or confirmation_cycles < 2:
            raise ValueError("confirmation_cycles must be >= 2")
        self.confirmation_cycles = confirmation_cycles
        if not isfinite(max_gap_seconds) or max_gap_seconds <= 0:
            raise ValueError("max_gap_seconds must be finite and positive")
        self.max_gap_seconds = max_gap_seconds
        self.blocked = False
        self.reason = ""
        self.safe_cycles = 0
        self._last_safe_at: datetime | None = None
        self._last_evaluated_at: datetime | None = None
        self._last_unsafe_at: datetime | None = None

    def update(self, feedback: MinuteNetworkFeedback, arbiter: ActuationArbiter,
               available: Mapping[str, float], actual: Mapping[str, float], *,
               rearm_requested: bool = False) -> bool:
        """Return True only for an explicit, limit-preserving rearm transition."""
        replayed = self._last_evaluated_at is not None and feedback.evaluated_at < self._last_evaluated_at
        predates_failure = (self.blocked and self._last_unsafe_at is not None
                            and feedback.observed_at is not None and feedback.observed_at < self._last_unsafe_at)
        safe = feedback.recovery_safe and not replayed and not predates_failure
        if not replayed:
            self._last_evaluated_at = feedback.evaluated_at
        if not safe:
            self.blocked = True
            self.reason = ("network_feedback_replayed_or_predates_failure" if replayed or predates_failure else
                           "network_safety_unknown:" + ";".join(feedback.reasons)
                           if not feedback.valid else "network_voltage_or_capacity_violation")
            self._last_unsafe_at = self._last_evaluated_at
            self.safe_cycles = 0
            self._last_safe_at = None
        elif feedback.observed_at is not None and (
            self._last_safe_at is None or feedback.observed_at > self._last_safe_at
        ):
            if self._last_safe_at is not None and (feedback.observed_at - self._last_safe_at).total_seconds() > self.max_gap_seconds:
                self.safe_cycles = 0
            self.safe_cycles += 1
            self._last_safe_at = feedback.observed_at
        if not self.blocked:
            return False
        effective = {r.station_id: r.effective_cap_mw for r in arbiter.resolve(available)}
        freeze = []
        for station, cap in effective.items():
            measured = actual.get(station)
            # Unknown actual output retains the previous effective cap; no fictitious zero.
            if measured is not None and isfinite(measured) and measured >= 0:
                cap = min(cap, measured)
            freeze.append(StationCapIntent(self.owner, station, CapIntentAction.SET_CAP,
                                           absolute_cap_mw=cap,
                                           decision_id=f"network-freeze:{feedback.evaluated_at.isoformat()}"))
        arbiter.apply(available, freeze)
        if not (rearm_requested and safe and self.safe_cycles >= self.confirmation_cycles):
            return False
        group_caps = arbiter.owner_caps("group_control")
        # Install receiving ownership before releasing network ownership.
        arbiter.apply(available, tuple(StationCapIntent(
            "group_control", station, CapIntentAction.SET_CAP,
            absolute_cap_mw=min(cap, group_caps.get(station, cap)), decision_id="network-rearm-transfer",
        ) for station, cap in arbiter.owner_caps(self.owner).items()))
        arbiter.apply(available, tuple(StationCapIntent(
            self.owner, station, CapIntentAction.RELEASE_CAP, decision_id="network-rearm-release",
        ) for station in arbiter.owner_caps(self.owner)))
        self.blocked = False
        self.reason = ""
        return True
