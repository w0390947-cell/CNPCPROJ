"""PCC safety allocation without direct device-state mutation.

The coordinator is deliberately narrow: PV curtailment is confirmed by the PV
actuation path first, and only the remaining safety deficit is converted into a
wind/storage *target*.  The target is submitted transactionally to the
Shancheng device controller.  This module never claims that a requested target
has been executed; a plant/field adapter must apply the returned setpoints and
provide acknowledgement and telemetry on the next snapshot.
"""

from __future__ import annotations

from math import isfinite

from .control_contracts import PccSafetyConstraint, SafetyAllocationResult
from .shancheng_control import (
    CommandDisposition,
    LegacyCommandFactory,
    ShanchengControlDecision,
    ShanchengController,
    ShanchengTelemetry,
    calculate_shancheng_capabilities,
)


_POWER_TOLERANCE = 1e-9


class ShanchengSafetyCoordinator:
    """Own and latch the hard-safety wind/storage target for one microgrid.

    The coordinator is the only hard-protection caller of
    :class:`ShanchengController`.  It converts an absolute PCC import floor to
    the controller's separate wind-plus-storage target semantics and records
    any capability shortfall explicitly.
    """

    def __init__(
        self,
        controller: ShanchengController | None = None,
        *,
        source_id: str = "hard-protection",
        source_epoch: str | None = None,
    ) -> None:
        self.controller = controller or ShanchengController()
        self.command_factory = LegacyCommandFactory(
            source_id=source_id,
            source_epoch=source_epoch,
            validity_seconds=60.0,
        )
        self.command_factory.authorize(self.controller)
        self._active_target_cap_mw: float | None = None
        self._last_allocation: SafetyAllocationResult | None = None

    @property
    def active(self) -> bool:
        return self._active_target_cap_mw is not None

    @property
    def active_target_cap_mw(self) -> float | None:
        return self._active_target_cap_mw

    @property
    def last_allocation(self) -> SafetyAllocationResult | None:
        return self._last_allocation

    def release(self) -> None:
        """Release this owner's cap without asserting a device-side value.

        The upstream schedule becomes the active owner after release.  The
        Shancheng controller's internal history is intentionally retained for
        audit/replay protection; it is simply no longer selected as writer.
        """

        self._active_target_cap_mw = None
        self._last_allocation = None

    def track(
        self,
        telemetry: ShanchengTelemetry,
    ) -> ShanchengControlDecision | None:
        """Re-evaluate a latched target against the latest device snapshot."""

        if not self.active:
            return None
        return self.controller.step(telemetry)

    def enforce(
        self,
        telemetry: ShanchengTelemetry,
        *,
        decision_id: str,
    ) -> ShanchengControlDecision | None:
        """Renew a still-owned hard target with a fresh sequenced command."""

        if not self.active:
            return None
        command = self.command_factory.make(
            now_utc=telemetry.observed_at_utc,
            message_id=decision_id,
            active_target_mw=self._active_target_cap_mw,
        )
        return self.controller.step(telemetry, command)

    def allocate(
        self,
        telemetry: ShanchengTelemetry,
        constraint: PccSafetyConstraint,
        *,
        pcc_after_confirmed_pv_mw: float,
        confirmed_pv_reduction_mw: float,
    ) -> tuple[SafetyAllocationResult, ShanchengControlDecision | None]:
        """Allocate the post-PV deficit through the Shancheng controller.

        ``confirmed_pv_reduction_mw`` is an achieved/plant value, not merely a
        cap request.  A lower feasible wind/storage target is selected only in
        the known internal no-reverse direction.  Any remaining deficit is
        reported instead of being subtracted from a measured wind value.
        """

        pcc_after_pv = float(pcc_after_confirmed_pv_mw)
        pv_reduction = float(confirmed_pv_reduction_mw)
        if not isfinite(pcc_after_pv):
            raise ValueError("pcc_after_confirmed_pv_mw must be finite")
        if not isfinite(pv_reduction) or pv_reduction < 0.0:
            raise ValueError("confirmed_pv_reduction_mw must be nonnegative and finite")

        pcc_before = pcc_after_pv - pv_reduction
        required_total = max(
            0.0, constraint.minimum_import_mw - pcc_before
        )
        residual = max(
            0.0, constraint.minimum_import_mw - pcc_after_pv
        )
        if residual <= _POWER_TOLERANCE:
            result = SafetyAllocationResult(
                decision_id=constraint.decision_id,
                minimum_import_mw=constraint.minimum_import_mw,
                pcc_before_action_mw=pcc_before,
                required_reduction_mw=required_total,
                confirmed_pv_reduction_mw=pv_reduction,
                requested_wind_storage_reduction_mw=0.0,
                allocated_wind_storage_reduction_mw=0.0,
                unserved_reduction_mw=0.0,
                wind_storage_target_mw=self._active_target_cap_mw,
                command_accepted=True,
                reason="confirmed_pv_reduction_satisfied_constraint",
            )
            self._last_allocation = result
            return result, self.track(telemetry)

        capabilities = calculate_shancheng_capabilities(
            telemetry, self.controller.config
        )
        current_wind_storage = (
            sum(item.active_power_mw for item in telemetry.wind_turbines)
            + telemetry.storage.active_power_mw
        )
        minimum_target = capabilities.minimum_active_target_mw
        if not capabilities.active_valid or minimum_target is None:
            decision = self.controller.step(telemetry)
            result = SafetyAllocationResult(
                decision_id=constraint.decision_id,
                minimum_import_mw=constraint.minimum_import_mw,
                pcc_before_action_mw=pcc_before,
                required_reduction_mw=required_total,
                confirmed_pv_reduction_mw=pv_reduction,
                requested_wind_storage_reduction_mw=residual,
                allocated_wind_storage_reduction_mw=0.0,
                unserved_reduction_mw=residual,
                wind_storage_target_mw=None,
                command_accepted=False,
                reason="wind_storage_capability_invalid",
            )
            self._last_allocation = result
            return result, decision

        requested_target = max(0.0, current_wind_storage - residual)
        if self._active_target_cap_mw is not None:
            requested_target = min(requested_target, self._active_target_cap_mw)
        feasible_target = max(float(minimum_target), requested_target)
        allocated = max(0.0, current_wind_storage - feasible_target)
        unserved = max(0.0, residual - allocated)

        command = self.command_factory.make(
            now_utc=telemetry.observed_at_utc,
            message_id=constraint.decision_id,
            active_target_mw=feasible_target,
        )
        decision = self.controller.step(telemetry, command)
        accepted = decision.active_disposition is CommandDisposition.ACCEPTED
        if accepted:
            self._active_target_cap_mw = feasible_target
            reason = (
                "wind_storage_target_accepted"
                if unserved <= _POWER_TOLERANCE
                else "wind_storage_target_saturated_with_unserved_deficit"
            )
        else:
            allocated = 0.0
            unserved = residual
            reason = "wind_storage_target_rejected"

        result = SafetyAllocationResult(
            decision_id=constraint.decision_id,
            minimum_import_mw=constraint.minimum_import_mw,
            pcc_before_action_mw=pcc_before,
            required_reduction_mw=required_total,
            confirmed_pv_reduction_mw=pv_reduction,
            requested_wind_storage_reduction_mw=residual,
            allocated_wind_storage_reduction_mw=allocated,
            unserved_reduction_mw=unserved,
            wind_storage_target_mw=feasible_target if accepted else None,
            command_accepted=accepted,
            reason=reason,
        )
        self._last_allocation = result
        return result, decision


__all__ = ["ShanchengSafetyCoordinator"]
