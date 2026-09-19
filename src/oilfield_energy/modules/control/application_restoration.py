"""Owned curtailment accounting and command-specific observed response evidence."""

from collections.abc import Mapping
from math import fsum, isfinite

from .contracts import RestorationCommand, RestorationEvidence, RestorationObservation

_POWER_TOLERANCE_MW = 1e-8
_TIME_TOLERANCE_MINUTES = 1e-8


class CurtailmentLedger:
    """Latch the baseline at curtailment; weather cannot retire an owned cap."""

    def __init__(self) -> None:
        self._references: dict[str, float] = {}

    def synchronize(
        self,
        baseline_mw: Mapping[str, float],
        owner_caps: Mapping[str, float],
        *,
        new_curtailment: bool = False,
    ) -> None:
        if set(owner_caps) - set(baseline_mw):
            raise ValueError("owned caps require a known baseline")
        if any(not isfinite(v) or v < 0 for v in (*baseline_mw.values(), *owner_caps.values())):
            raise ValueError("baseline and cap powers must be finite and nonnegative")
        self._references = {
            sid: (
                max(self._references.get(sid, 0.0), baseline_mw[sid], cap)
                if new_curtailment
                else self._references.get(sid, max(baseline_mw[sid], cap))
            )
            for sid, cap in owner_caps.items()
        }

    def references(self) -> dict[str, float]:
        return dict(self._references)

    def remaining_mw(self, owner_caps: Mapping[str, float]) -> float:
        if set(owner_caps) != set(self._references):
            raise ValueError("synchronize owned caps before reading the ledger")
        return fsum(
            max(0.0, reference - owner_caps[sid]) for sid, reference in self._references.items()
        )


class RestorationMonitor:
    """Latch any dwell-period ambiguity; pure, clock-injected and no device I/O."""

    def __init__(self, *, dwell_minutes: float, interval_minutes: float) -> None:
        if any(not isfinite(v) or v <= 0 for v in (dwell_minutes, interval_minutes)):
            raise ValueError("restoration timing must be positive and finite")
        self._dwell = dwell_minutes
        self._interval = interval_minutes
        self._command: RestorationCommand | None = None
        self._last_time = 0.0
        self._invalid_reason = ""

    def cancel(self) -> None:
        self._command = None
        self._invalid_reason = ""

    def begin(self, command: RestorationCommand) -> None:
        self._command = command
        self._last_time = command.before.time_minutes
        self._invalid_reason = ""
        if not command.before.valid:
            self._invalid_reason = "invalid_command_baseline"
        elif abs(command.cap_released_mw - command.requested_mw) > _POWER_TOLERANCE_MW:
            self._invalid_reason = "cap_release_does_not_match_request"

    def observe(self, observation: RestorationObservation) -> RestorationEvidence | None:
        command = self._command
        if command is None:
            return None
        elapsed = observation.time_minutes - self._last_time
        reason = ""
        if abs(elapsed - self._interval) > _TIME_TOLERANCE_MINUTES:
            reason = "restoration_observation_gap_or_replay"
        elif not observation.valid:
            reason = "restoration_observation_invalid"
        elif observation.acknowledged_command_id != command.command_id:
            reason = "restoration_command_not_acknowledged"
        before = {s.station_id: s for s in command.before.stations}
        after = {s.station_id: s for s in observation.stations}
        if set(before) != set(after):
            reason = reason or "restoration_station_coverage_changed"
        else:
            for sid, previous in before.items():
                current = after[sid]
                if (
                    abs(current.baseline_mw - previous.baseline_mw) > _POWER_TOLERANCE_MW
                    or abs(current.available_mw - previous.available_mw) > _POWER_TOLERANCE_MW
                    or dict(current.other_caps) != dict(previous.other_caps)
                ):
                    reason = reason or "restoration_exogenous_context_changed"
        self._invalid_reason = self._invalid_reason or reason
        self._last_time = observation.time_minutes
        delta = None
        achieved = None
        status = "pending"
        reason = "restoration_dwell_pending"
        if self._invalid_reason:
            status = "unknown"
            reason = self._invalid_reason
        elif (
            observation.time_minutes - command.before.time_minutes
            >= self._dwell - _TIME_TOLERANCE_MINUTES
        ):
            status = "observed"
            delta = fsum(s.actual_mw for s in observation.stations) - fsum(
                s.actual_mw for s in command.before.stations
            )
            achieved = max(0.0, delta)
            reason = "device_output_change_observed"
        return RestorationEvidence(
            command.command_id,
            command.before.time_minutes,
            observation.time_minutes,
            command.requested_mw,
            command.cap_released_mw,
            status,
            delta,
            achieved,
            reason,
        )
