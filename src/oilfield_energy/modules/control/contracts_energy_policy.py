"""Feedback used to complete an explicitly accepted storage energy task."""

from pydantic import Field

from .contracts_tube import TubeCommand, TubeValue


class EnergyReturnPolicy(TubeValue, frozen=True):
    base: TubeCommand
    target_mwh: float
    charge_limit_mw: float = Field(ge=0)
    discharge_limit_mw: float = Field(ge=0)
