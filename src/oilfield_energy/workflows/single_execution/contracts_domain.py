"""Explicit boundary inputs for the unified single-microgrid algorithm."""

from pydantic import BaseModel, ConfigDict, Field, model_validator

from oilfield_energy.modules.control.contracts import TubeState
from oilfield_energy.modules.power_flow.contracts import PowerInterval


class SingleValue(BaseModel, frozen=True):
    model_config = ConfigDict(frozen=True, extra="forbid", allow_inf_nan=False)


class BoundedBusLoad(SingleValue, frozen=True):
    bus: str = Field(min_length=1)
    p_mw: PowerInterval
    q_mvar: PowerInterval


class FirmGeneration(SingleValue, frozen=True):
    resource_id: str = Field(min_length=1)
    minimum_available_mw: float = Field(ge=0)


class SingleBoundaryRequest(SingleValue, frozen=True):
    """Absolute envelopes remain valid through execution AND terminal continuation.

    These inputs override research forecasts within the same unified algorithm.
    Fixed topology, authority and the declared unified response model apply.
    """

    source: str = Field(min_length=1)
    loads: tuple[BoundedBusLoad, ...]
    generation: tuple[FirmGeneration, ...]
    target_p_mw: float
    target_q_mvar: float
    initial_state: TubeState | None = None
    initial_resource_ids: tuple[str, ...] = ()

    @model_validator(mode="after")
    def unique(self) -> "SingleBoundaryRequest":
        if (
            not self.loads
            or len({b.bus for b in self.loads}) != len(self.loads)
            or len({g.resource_id for g in self.generation}) != len(self.generation)
        ):
            raise ValueError("disturbance envelopes require unique complete identities")
        if any(b.p_mw.low < 0 for b in self.loads):
            raise ValueError("gross load P must be nonnegative")
        if (self.initial_state is None) != (not self.initial_resource_ids):
            raise ValueError(
                "declared initial state requires its resource identity axis"
            )
        if self.initial_state is not None and (
            len(set(self.initial_resource_ids)) != len(self.initial_resource_ids)
            or len(self.initial_state.p_mw) != len(self.initial_resource_ids)
            or len(self.initial_state.q_mvar) != len(self.initial_resource_ids)
        ):
            raise ValueError("initial state resource axis differs")
        return self
