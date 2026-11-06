"""Admitted metrology and boundary alignment limits; all units are normalized."""

from typing import Annotated, Literal

from pydantic import AwareDatetime, Field, model_validator

from .contracts_values import Bounds, Name, Nonnegative, Positive, Value


class MeasurementLimit(Value):
    source_kind: Literal["measured", "estimated"]
    source_ref: Name
    calibration_revision: Name
    max_absolute_error: Nonnegative
    max_timestamp_error_seconds: Nonnegative
    alignment_rate_per_second: Nonnegative
    provenance_ref: Name


class ObservationUncertaintyPolicy(Value):
    root_voltage_bounds_pu: Bounds
    profile: Literal["bounded_observation_v1"]
    revision: Name
    clock_revision: Name
    provenance_ref: Name
    valid_from: AwareDatetime
    valid_until: AwareDatetime
    boundary_radius_seconds: Positive
    measurements: Annotated[
        tuple[MeasurementLimit, ...], Field(min_length=1, max_length=4096)
    ]

    @model_validator(mode="after")
    def validity(self) -> "ObservationUncertaintyPolicy":
        if (
            self.valid_from >= self.valid_until
            or self.root_voltage_bounds_pu.low <= 0
            or len({x.source_ref for x in self.measurements}) != len(self.measurements)
        ):
            raise ValueError(
                "Uncertainty policy needs unique sources and ordered validity"
            )
        for value in (
            self.boundary_radius_seconds,
            *(m.max_timestamp_error_seconds for m in self.measurements),
        ):
            if value >= 60 or round(value * 1_000_000) / 1_000_000 != value:
                raise ValueError(
                    "Uncertainty time bounds require microsecond precision"
                )
        return self
