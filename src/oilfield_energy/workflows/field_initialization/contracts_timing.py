"""Explicit bounded-time assumptions and finite, conservative execution proofs."""

from typing import Annotated, Literal

from pydantic import AwareDatetime, Field, StrictBool, model_validator

from oilfield_energy.modules.control.contracts import TubeBox, TubeCommand

from .contracts_values import Name, Nonnegative, Positive, Value


class ResourceTiming(Value):
    zero_p_is_exact: StrictBool = False
    zero_q_is_exact: StrictBool = False
    settled_p_error_mw: Nonnegative = 0.0
    settled_q_error_mvar: Nonnegative = 0.0
    resource_id: Name
    settling_max_seconds: Nonnegative
    provenance_ref: Name


class BoundedTimingPolicy(Value):
    profile: Literal["bounded_monotone_v1", "bounded_uncertain_v1"]
    revision: Name
    provenance_ref: Name
    response_semantics: Literal[
        "monotone_no_overshoot_exact_settling", "monotone_no_overshoot_bounded_settling"
    ]
    clock_error_bound_seconds: Nonnegative
    prepare_max_seconds: Positive
    transport_max_seconds: Positive
    apply_not_before_seconds: Positive
    apply_not_after_seconds: Positive
    application_skew_max_seconds: Nonnegative
    snapshot_delivery_max_seconds: Positive
    resources: Annotated[
        tuple[ResourceTiming, ...], Field(min_length=1, max_length=128)
    ]

    @model_validator(mode="after")
    def clock(self) -> "BoundedTimingPolicy":
        values = (
            self.prepare_max_seconds,
            self.transport_max_seconds,
            self.apply_not_before_seconds,
            self.apply_not_after_seconds,
            self.application_skew_max_seconds,
            self.snapshot_delivery_max_seconds,
            self.clock_error_bound_seconds,
            *(r.settling_max_seconds for r in self.resources),
        )
        if any(x >= 60 or round(x * 1_000_000) / 1_000_000 != x for x in values):
            raise ValueError(
                "Timing bounds must be exactly representable on the microsecond clock"
            )
        before = round(self.apply_not_before_seconds * 1_000_000)
        after = round(self.apply_not_after_seconds * 1_000_000)
        if (
            (
                self.profile == "bounded_monotone_v1"
                and (
                    self.clock_error_bound_seconds != 0
                    or self.response_semantics != "monotone_no_overshoot_exact_settling"
                    or any(
                        r.settled_p_error_mw or r.settled_q_error_mvar
                        for r in self.resources
                    )
                )
            )
            or (
                self.profile == "bounded_uncertain_v1"
                and self.response_semantics != "monotone_no_overshoot_bounded_settling"
            )
            or before > after
            or after >= 60_000_000
            or round(self.prepare_max_seconds * 1_000_000)
            + round(self.transport_max_seconds * 1_000_000)
            + round(self.clock_error_bound_seconds * 1_000_000)
            > before
            or before <= round(self.clock_error_bound_seconds * 1_000_000)
            or round(self.application_skew_max_seconds * 1_000_000) > after - before
            or len({r.resource_id for r in self.resources}) != len(self.resources)
            or any(
                after
                + round(
                    (r.settling_max_seconds + self.clock_error_bound_seconds)
                    * 1_000_000
                )
                >= 60_000_000
                for r in self.resources
            )
        ):
            raise ValueError(
                "Inconsistent bounded application, preparation or settling windows"
            )
        return self


class TimedCycleProof(Value):
    profile: Literal["bounded_monotone_v1", "bounded_uncertain_v1"] = (
        "bounded_monotone_v1"
    )
    clock_error_bound_seconds: Nonnegative = 0.0
    clock_revision: str | None = None
    physical_apply_earliest_at: AwareDatetime | None = None
    physical_apply_latest_at: AwareDatetime | None = None
    profile_revision: Name
    energy_task_guarantee: Literal["not_required", "maintained", "unproven"]
    application_skew_max_seconds: Nonnegative
    start_step: int = Field(ge=0)
    apply_not_before_at: AwareDatetime
    apply_not_after_at: AwareDatetime
    prepare_deadline_at: AwareDatetime
    dispatch_deadline_at: AwareDatetime
    endpoint: TubeBox
    path: TubeBox
    continuation_commands: tuple[TubeCommand, ...]
    continuation_endpoints: tuple[TubeBox, ...]
    guaranteed_until: AwareDatetime
    proof_digest: Name
