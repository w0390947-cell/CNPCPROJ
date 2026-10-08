"""Finite feedback policies whose uncertainty can be enclosed before execution."""

from pydantic import Field, model_validator

from .contracts_tube import TubeBox, TubeCommand, TubeValue


class BalancePolicy(TubeValue, frozen=True):
    base: TubeCommand
    p_regulator: int | None = Field(default=None, ge=0)
    q_regulator: int | None = Field(default=None, ge=0)
    p_participation: tuple[float, ...] = ()
    q_participation: tuple[float, ...] = ()
    net_p_mw: float | None = None
    net_q_mvar: float | None = None

    @model_validator(mode="after")
    def regulators(self) -> "BalancePolicy":
        if (not any(self.weights(False))) != (self.net_p_mw is None) or (
            (not any(self.weights(True))) != (self.net_q_mvar is None)
        ):
            raise ValueError(
                "balance policy requires each regulator and its net demand"
            )
        n = len(self.base.p_mw)
        if len(self.base.q_mvar) != n or any(
            i is not None and i >= n for i in (self.p_regulator, self.q_regulator)
        ):
            raise ValueError("balance regulator differs from the resource axis")
        for regulator, weights in (
            (self.p_regulator, self.p_participation),
            (self.q_regulator, self.q_participation),
        ):
            if weights and (
                regulator is not None
                or len(weights) != n
                or min(weights) < 0
                or not any(weights)
            ):
                raise ValueError(
                    "feedback participation must be nonnegative, nonempty and unambiguous"
                )
        return self

    def weights(self, reactive: bool) -> tuple[float, ...]:
        """Nonnegative participation scores; the algorithm normalizes exactly."""
        values = self.q_participation if reactive else self.p_participation
        regulator = self.q_regulator if reactive else self.p_regulator
        return values or tuple(
            float(i == regulator) for i in range(len(self.base.p_mw))
        )


class PolicyTransition(TubeValue, frozen=True):
    endpoint: TubeBox
    path: TubeBox
    balance_p_error_mw: float = Field(default=0, ge=0)
    balance_q_error_mvar: float = Field(default=0, ge=0)
