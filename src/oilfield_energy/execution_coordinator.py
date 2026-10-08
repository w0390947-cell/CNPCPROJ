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
from typing import Literal

from .control_contracts import PccSafetyConstraint, SafetyAllocationResult
from .modules.control.api import project_protection_target
from .modules.control.contracts import ProtectionTargetRecord
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
        self._last_target_record: ProtectionTargetRecord | None = None

    @property
    def active(self) -> bool:
        return self._active_target_cap_mw is not None

    @property
    def active_target_cap_mw(self) -> float | None:
        """Retained safety requirement, even when current capability cannot meet it."""
        return self._active_target_cap_mw

    @property
    def last_allocation(self) -> SafetyAllocationResult | None:
        """Historical allocation with PCC input; renewal does not refresh its facts."""
        return self._last_allocation

    @property
    def last_target_record(self) -> ProtectionTargetRecord | None:
        return self._last_target_record

    def release(self) -> None:
        """Release this owner's cap without asserting a device-side value.

        The upstream schedule becomes the active owner after release.  The
        Shancheng controller's internal history is intentionally retained for
        audit/replay protection; it is simply no longer selected as writer.
        """

        self._active_target_cap_mw = None
        self._last_allocation = None
        self._last_target_record = None

    def track(
        self,
        telemetry: ShanchengTelemetry,
    ) -> ShanchengControlDecision | None:
        """Re-evaluate a latched target against the latest device snapshot."""

        if not self.active:
            return None
        return self._evaluate_target(
            telemetry,
            required_cap_mw=self._active_target_cap_mw,
            action="observation",
        )

    def enforce(
        self,
        telemetry: ShanchengTelemetry,
        *,
        decision_id: str,
    ) -> ShanchengControlDecision | None:
        """Renew a still-owned hard target with a fresh sequenced command."""

        if not self.active:
            return None
        return self._evaluate_target(
            telemetry,
            required_cap_mw=self._active_target_cap_mw,
            action="renewal",
            decision_id=decision_id,
        )

    def _evaluate_target(
        self,
        telemetry: ShanchengTelemetry,
        *,
        required_cap_mw: float | None,
        action: Literal["allocation", "renewal", "observation"],
        decision_id: str | None = None,
    ) -> ShanchengControlDecision:
        previous_cap = self._active_target_cap_mw
        capabilities = calculate_shancheng_capabilities(
            telemetry, self.controller.config
        )
        minimum = capabilities.minimum_active_target_mw
        maximum = capabilities.maximum_active_target_mw
        projection = (
            project_protection_target(required_cap_mw, minimum, maximum)
            if required_cap_mw is not None
            and capabilities.active_valid
            and minimum is not None
            and maximum is not None
            else None
        )
        command = None
        if action != "observation" and projection is not None:
            command = self.command_factory.make(
                now_utc=telemetry.observed_at_utc,
                message_id=decision_id,
                active_target_mw=projection.target_mw,
            )
        decision = self.controller.step(telemetry, command)
        accepted = (
            decision.active_disposition is CommandDisposition.ACCEPTED
            if command is not None
            else None
        )
        # A newly requested tighter requirement survives command rejection.
        # Only acceptance can additionally tighten it to the projected target.
        # An unattainable cap is never raised to a larger legal command.
        retained = required_cap_mw
        if accepted and projection is not None:
            retained = min(projection.required_cap_mw, projection.target_mw)
        reason = (
            "observation_only"
            if action == "observation"
            else "capability_invalid_no_command"
            if projection is None
            else "command_not_accepted"
            if not accepted
            else "minimum_legal_target_with_capability_shortfall"
            if projection.capability_shortfall_mw > _POWER_TOLERANCE
            else "target_accepted_execution_unconfirmed"
        )
        record = ProtectionTargetRecord(
            snapshot_id=telemetry.snapshot_id,
            observed_at_utc=telemetry.observed_at_utc,
            action=action,
            decision_id=command.message_id if command is not None else decision_id,
            command_source_id=command.source_id if command is not None else None,
            command_source_epoch=command.source_epoch if command is not None else None,
            command_sequence=command.active_request.sequence
            if command is not None and command.active_request is not None
            else None,
            command_valid_until_utc=command.valid_until_utc
            if command is not None
            else None,
            previous_cap_mw=previous_cap,
            required_cap_mw=required_cap_mw,
            retained_cap_mw=retained,
            projection=projection,
            submitted_target_mw=projection.target_mw
            if command is not None and projection
            else None,
            command_accepted=accepted,
            controller_target_mw=decision.active_target_mw,
            controller_disposition=decision.active_disposition.value,
            reason=reason,
        )
        self._active_target_cap_mw = retained
        self._last_target_record = record
        return decision

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
        required_total = max(0.0, constraint.minimum_import_mw - pcc_before)
        residual = max(0.0, constraint.minimum_import_mw - pcc_after_pv)
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
                wind_storage_target_mw=None,
                command_accepted=False,
                reason="pcc_floor_currently_satisfied_no_new_command",
            )
            decision = self.track(telemetry)
            self._last_allocation = result
            return result, decision

        capabilities = calculate_shancheng_capabilities(
            telemetry, self.controller.config
        )
        current_wind_storage = (
            sum(item.active_power_mw for item in telemetry.wind_turbines)
            + telemetry.storage.active_power_mw
        )
        minimum_target = capabilities.minimum_active_target_mw
        maximum_target = capabilities.maximum_active_target_mw
        if (
            not capabilities.active_valid
            or minimum_target is None
            or maximum_target is None
        ):
            decision = self._evaluate_target(
                telemetry,
                required_cap_mw=self._active_target_cap_mw,
                action="allocation",
                decision_id=constraint.decision_id,
            )
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
        decision = self._evaluate_target(
            telemetry,
            required_cap_mw=requested_target,
            action="allocation",
            decision_id=constraint.decision_id,
        )
        record = self._last_target_record
        assert record is not None and record.projection is not None
        feasible_target = record.projection.target_mw
        allocated = max(0.0, current_wind_storage - feasible_target)
        unserved = max(0.0, residual - allocated)
        accepted = record.command_accepted is True
        if accepted:
            reason = (
                "wind_storage_target_saturated_with_unserved_deficit"
                if unserved > _POWER_TOLERANCE
                else "wind_storage_target_accepted_with_retained_cap_shortfall"
                if record.projection.capability_shortfall_mw > _POWER_TOLERANCE
                else "wind_storage_target_accepted"
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
