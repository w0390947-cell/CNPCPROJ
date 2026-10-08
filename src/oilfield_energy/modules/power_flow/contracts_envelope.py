"""Contracts for a conditional, all-inputs radial AC enclosure."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from oilfield_energy.kernel.intervals import Interval as PowerInterval


class EnvelopeValue(BaseModel, frozen=True):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


class EnvelopeBranch(EnvelopeValue, frozen=True):
    parent: int = Field(ge=0)
    child: int = Field(ge=0)
    r_pu: float = Field(ge=0)
    x_pu: float = Field(ge=0)
    tap: float = Field(gt=0)
    rating_mva: float = Field(gt=0)


class RadialEnvelope(EnvelopeValue, frozen=True):
    network_id: str = Field(min_length=1)
    buses: tuple[str, ...]
    branches: tuple[EnvelopeBranch, ...]
    slack: int = Field(ge=0)
    base_mva: float = Field(gt=0)
    slack_voltage_pu: float = Field(gt=0)
    shunt_q_mvar: tuple[float, ...]
    voltage_min_pu: float = Field(gt=0)
    voltage_max_pu: float = Field(gt=0)
    pcc_min_mw: float = Field(ge=0)
    pcc_max_mw: float = Field(gt=0)
    pf_min: float = Field(gt=0, le=1)

    @model_validator(mode="after")
    def radial(self) -> "RadialEnvelope":
        n = len(self.buses)
        if (
            not n
            or len(set(self.buses)) != n
            or self.slack >= n
            or len(self.shunt_q_mvar) != n
            or self.voltage_min_pu >= self.voltage_max_pu
            or self.pcc_min_mw >= self.pcc_max_mw
        ):
            raise ValueError("invalid network envelope")
        seen = {self.slack}
        for line in self.branches:
            if line.parent not in seen or line.child in seen or line.child >= n:
                raise ValueError(
                    "envelope requires a topologically ordered radial tree"
                )
            seen.add(line.child)
        if len(seen) != n:
            raise ValueError("disconnected envelope")
        return self


class ACBoxCertificate(EnvelopeValue, frozen=True):
    """Bounds imply existence and uniqueness only inside the recorded voltage discs."""

    model: Literal["radial-ac-disc-v1"] = "radial-ac-disc-v1"
    certified: bool
    reason: str = ""
    voltage_center: tuple[tuple[float, float], ...] = ()
    voltage_radius_pu: float | None = None
    contraction_upper: float | None = None
    high_voltage_uniqueness_upper: float | None = None
    voltage_bounds: tuple[PowerInterval, ...] = ()
    branch_mva_upper: tuple[float, ...] = ()
    branch_current_pu_bounds: tuple[PowerInterval, ...] = ()
    pcc_p_mw: PowerInterval | None = None
    pcc_q_mvar: PowerInterval | None = None
    balance_scope: Literal["independent_bus_box", "aggregate_constrained_box"] = (
        "independent_bus_box"
    )
    aggregate_p_mw: PowerInterval | None = None
    aggregate_q_mvar: PowerInterval | None = None

    @model_validator(mode="after")
    def complete(self) -> "ACBoxCertificate":
        if self.balance_scope == "aggregate_constrained_box" and (
            self.aggregate_p_mw is None or self.aggregate_q_mvar is None
        ):
            raise ValueError(
                "correlated AC certificate requires its aggregate constraints"
            )
        if self.certified and (
            not self.voltage_center
            or len(self.voltage_center) != len(self.voltage_bounds)
            or self.voltage_radius_pu is None
            or self.voltage_radius_pu < 0
            or self.contraction_upper is None
            or not 0 <= self.contraction_upper < 1
            or self.high_voltage_uniqueness_upper is None
            or not 0 <= self.high_voltage_uniqueness_upper < 1
            or self.pcc_p_mw is None
            or self.pcc_q_mvar is None
        ):
            raise ValueError("AC box certificate is incomplete")
        return self
