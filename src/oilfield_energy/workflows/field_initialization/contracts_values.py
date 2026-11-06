"""Transport-neutral values; no clocks, storage, or numerical engines."""

from datetime import datetime
from typing import Annotated, Literal, get_args

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StrictFloat,
    StrictInt,
    ValidationInfo,
    field_validator,
    model_validator,
)

Name = Annotated[str, Field(min_length=1, max_length=160, pattern=r"\S")]
Nonnegative = Annotated[StrictFloat, Field(ge=0)]
Positive = Annotated[StrictFloat, Field(gt=0)]


class Value(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)

    @field_validator("*", mode="before")
    @classmethod
    def explicit_wire_types(cls, value: object, info: ValidationInfo) -> object:
        assert info.field_name is not None
        annotation = cls.model_fields[info.field_name].annotation
        alternatives = (annotation, *get_args(annotation))
        if (
            AwareDatetime in alternatives
            and value is not None
            and not isinstance(value, (str, datetime))
        ):
            raise ValueError(
                "timestamp must be an ISO string or timezone-aware datetime"
            )
        if annotation is Literal[False] and type(value) is not bool:
            raise ValueError("boolean required")
        return value


class ArtifactReference(Value):
    artifact_id: Name
    revision: Name
    sha256: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]


class Bounds(Value):
    low: StrictFloat
    high: StrictFloat

    @model_validator(mode="after")
    def ordered(self) -> "Bounds":
        if self.low > self.high:
            raise ValueError("low must not exceed high")
        return self


class TimestampUncertainty(Value):
    error_seconds: Nonnegative
    clock_revision: Name
    provenance_ref: Name
    valid_from: AwareDatetime
    valid_until: AwareDatetime

    @model_validator(mode="after")
    def validity(self) -> "TimestampUncertainty":
        if (
            self.valid_from >= self.valid_until
            or self.error_seconds >= 60
            or round(self.error_seconds * 1_000_000) / 1_000_000 != self.error_seconds
        ):
            raise ValueError(
                "Clock evidence needs ordered validity and microsecond precision"
            )
        return self


class MeasurementUncertainty(Value):
    absolute_error: Nonnegative
    calibration_revision: Name
    provenance_ref: Name
    valid_from: AwareDatetime
    valid_until: AwareDatetime
    timestamp: TimestampUncertainty

    @model_validator(mode="after")
    def validity(self) -> "MeasurementUncertainty":
        if self.valid_from >= self.valid_until:
            raise ValueError("Calibration validity must be ordered")
        return self


class MeasuredValue(Value):
    uncertainty: MeasurementUncertainty | None = None
    value: StrictFloat | None
    observed_at: AwareDatetime
    quality: Literal["good", "bad", "questionable", "unknown"]
    source_kind: Literal["measured", "estimated"]
    source_ref: Name
    value_semantics: Literal["instantaneous", "interval_average", "interval_energy"]
    interval_start: AwareDatetime | None = None
    interval_end: AwareDatetime | None = None

    @model_validator(mode="after")
    def interval(self) -> "MeasuredValue":
        if self.value_semantics != "instantaneous" and (
            self.interval_start is None
            or self.interval_end is None
            or self.interval_start >= self.interval_end
        ):
            raise ValueError("non-instantaneous values require an ordered interval")
        return self


class BusLoad(Value):
    bus_id: Name
    p_mw: MeasuredValue
    q_mvar: MeasuredValue
    scope_ref: Name


class DeviceState(Value):
    status_timestamp_uncertainty: TimestampUncertainty | None = None
    resource_id: Name
    p_mw: MeasuredValue
    q_mvar: MeasuredValue
    energy_mwh: MeasuredValue | None = None
    online: StrictBool
    remote_enabled: StrictBool
    blocked: StrictBool
    status_observed_at: AwareDatetime


class Availability(Value):
    resource_id: Name
    available_p_mw: MeasuredValue
    method_ref: Name


class Pcc(Value):
    bus_id: Name
    p_import_mw: MeasuredValue
    q_import_mvar: MeasuredValue
    voltage_pu: MeasuredValue


class HeldSetpoint(Value):
    timestamp_uncertainty: TimestampUncertainty | None = None
    held_since: AwareDatetime | None = None
    since_uncertainty: TimestampUncertainty | None = None
    resource_id: Name
    p_mw: StrictFloat
    q_mvar: StrictFloat
    observed_at: AwareDatetime
    source_ref: Name


class SnapshotData(Value):
    snapshot_id: Name
    sequence: Annotated[StrictInt, Field(ge=0)]
    snapshot_at: AwareDatetime
    received_at: AwareDatetime
    model_revision: Name
    mapping_revision: Name
    operating_mode_id: Name
    permission_revision: Name
    bus_loads: Annotated[tuple[BusLoad, ...], Field(min_length=1, max_length=512)]
    device_states: Annotated[
        tuple[DeviceState, ...], Field(min_length=1, max_length=128)
    ]
    renewable_availability: tuple[Availability, ...]
    pcc: Pcc
    operating_state_ref: Name
    held_setpoints: tuple[HeldSetpoint, ...] | None = None


class Snapshot(SnapshotData):
    context_id: None


class Authority(Value):
    owner_id: Name
    epoch: Annotated[StrictInt, Field(ge=0)]
    valid_until: AwareDatetime
    permission_revision: Name
    write_enabled: StrictBool = False


class InterfaceError(Value):
    code: str
    message: str
    field_path: str | None = None
    resource_id: str | None = None
    retryable: StrictBool = False
    required_action: str = "Correct the input or resolve the stated prerequisite."


class InitializationFailure(ValueError):
    def __init__(self, code: str, message: str, field_path: str | None = None) -> None:
        super().__init__(message)
        self.error = InterfaceError(code=code, message=message, field_path=field_path)
