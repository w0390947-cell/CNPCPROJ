"""Nominal single-microgrid P/Q transition witnesses; not AC safety certificates."""

from typing import Literal
from fractions import Fraction as F

from pydantic import BaseModel, ConfigDict, Field, model_validator

from oilfield_energy.modules.control.contracts import (
    TubeCommand,
    TubeDeviceModel,
    TubeState,
)


class PQPlanningInput(BaseModel, frozen=True):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    version: Literal["single-pq-planning-v2"] = "single-pq-planning-v2"
    start_step: int = Field(default=0, ge=0)
    devices: TubeDeviceModel
    initial: TubeState
    planning_steps: int = Field(gt=0)
    planning_minutes: int = Field(gt=0)
    transition_minutes: int = Field(gt=0)
    # Each boundary from current minute through the window end, in resource order.
    p_ceiling_mw: tuple[tuple[float, ...], ...]
    polygon_sides: int = Field(ge=4)
    reserve_fraction: float = Field(ge=0, lt=1)
    # Construction margin for numerical/small-response differences, not a rating.
    ramp_utilization: float = Field(default=0.98, gt=0, le=1)
    settling_utilization: float = Field(default=0.98, gt=0, le=1)

    @model_validator(mode="after")
    def aligned(self) -> "PQPlanningInput":
        n = len(self.devices.resources)
        if (
            self.devices.version != "unified-device-v1"
            or self.devices.step_minutes != 1
            or self.transition_minutes > self.planning_minutes
            or len(self.initial.p_mw) != n
            or len(self.initial.q_mvar) != n
            or len(self.p_ceiling_mw) != self.planning_steps * self.planning_minutes + 1
            or any(len(v) != n for v in self.p_ceiling_mw)
            or any(
                not r.p.low <= p <= r.p.high
                for v in self.p_ceiling_mw
                for r, p in zip(self.devices.resources, v, strict=True)
            )
        ):
            raise ValueError("joint P/Q planning requires an aligned single ZOH model")
        if not self.devices.energy_mwh.contains(self.initial.energy_mwh):
            raise ValueError("joint P/Q initial energy outside physical bounds")
        for r, p, q, ceiling in zip(
            self.devices.resources,
            self.initial.p_mw,
            self.initial.q_mvar,
            self.p_ceiling_mw[0],
            strict=True,
        ):
            if r.kind == "svg" and (r.p.low != 0 or r.p.high != 0):
                raise ValueError("joint P/Q SVG requires zero active power")
            if (
                not r.p.low <= p <= ceiling
                or not r.q.low <= q <= r.q.high
                or F(p) ** 2 + F(q) ** 2 > F(r.s_mva) ** 2
                or (r.q_over_p is not None and abs(F(q)) > F(r.q_over_p) * F(p))
            ):
                raise ValueError(
                    f"joint P/Q actual initial capability: {r.resource_id}"
                )
        return self


class PQPlanningWitness(BaseModel, frozen=True):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    inputs: PQPlanningInput
    # Independent commands during each transition, then hold the plan endpoint.
    commands: tuple[tuple[TubeCommand, ...], ...]
    endpoints: tuple[TubeCommand, ...]

    @model_validator(mode="after")
    def aligned(self) -> "PQPlanningWitness":
        x = self.inputs
        n = len(x.devices.resources)
        if (
            len(self.commands) != x.planning_steps
            or len(self.endpoints) != x.planning_steps
            or any(len(c) != x.transition_minutes for c in self.commands)
            or any(
                len(c.p_mw) != n or len(c.q_mvar) != n
                for c in (*self.endpoints, *(v for w in self.commands for v in w))
            )
        ):
            raise ValueError("joint P/Q witness axes differ")
        return self
