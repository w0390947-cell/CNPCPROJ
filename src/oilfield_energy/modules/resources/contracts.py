"""Immutable device identity, wind reactive policy and SVG capability contracts."""

from dataclasses import dataclass
from enum import Enum
from math import isfinite, sqrt
from typing import Literal

ResourceKind = Literal["wind", "pv", "storage", "svg"]


class WindReactivePolicy(str, Enum):
    """Source-backed Q/P policy, independent of a device's absolute ratings."""

    GENERAL = "general-333"
    SHANCHENG = "shancheng-300"

    @property
    def ratio(self) -> float:
        return 0.333 if self is WindReactivePolicy.GENERAL else 0.30

    @property
    def source(self) -> str:
        return (
            "西交大-资料提供.pdf，第9页"
            if self is WindReactivePolicy.GENERAL
            else "山城微电网控制策略逻辑.pdf，第3、5页"
        )


@dataclass(frozen=True)
class WindReactiveCapability:
    """Intersect policy, absolute Q rating and converter capacity at actual P.

    Missing ratio/absolute Q is reserved for historical inputs which only
    declared a converter envelope. A zero ratio explicitly disables Q.
    """

    s_max_mva: float
    q_abs_over_p_max: float | None = None
    q_abs_max_mvar: float | None = None

    def __post_init__(self) -> None:
        if not isfinite(self.s_max_mva) or self.s_max_mva <= 0:
            raise ValueError("wind apparent capacity must be finite and positive")
        for value in (self.q_abs_over_p_max, self.q_abs_max_mvar):
            if value is not None and (not isfinite(value) or value < 0):
                raise ValueError("wind reactive limits must be finite and nonnegative")

    @property
    def absolute_limit_mvar(self) -> float:
        return (
            min(self.s_max_mva, self.q_abs_max_mvar)
            if self.q_abs_max_mvar is not None
            else self.s_max_mva
        )

    def limit_at(self, active_mw: float) -> float:
        if not isfinite(active_mw) or active_mw < 0:
            raise ValueError("wind active power must be finite and nonnegative")
        limit = min(self.absolute_limit_mvar, sqrt(max(0.0, self.s_max_mva**2 - active_mw**2)))
        if self.q_abs_over_p_max is not None:
            limit = min(limit, self.q_abs_over_p_max * active_mw)
        return limit


@dataclass(frozen=True)
class ResourceIdentity:
    resource_id: str
    bus_id: str
    kind: ResourceKind

    def __post_init__(self) -> None:
        if not self.resource_id.strip() or not self.bus_id.strip():
            raise ValueError("resource and bus identities must be nonempty")
        if self.kind not in ("wind", "pv", "storage", "svg"):
            raise ValueError("unsupported resource kind")


@dataclass(frozen=True)
class SvgCapability:
    """P=0 SVG: intersect declared signed Q bounds with the MVA circle.

    Declared bounds remain evidence; effective bounds are derived, never used
    to increase the nameplate rating. Zero output must remain feasible.
    """

    q_min_mvar: float
    q_max_mvar: float
    s_max_mva: float

    def __post_init__(self) -> None:
        if not all(isfinite(v) for v in (self.q_min_mvar, self.q_max_mvar, self.s_max_mva)):
            raise ValueError("SVG capability must be finite")
        if self.s_max_mva <= 0 or not self.q_min_mvar <= 0 <= self.q_max_mvar:
            raise ValueError("SVG requires positive MVA and Q bounds containing zero")

    @property
    def effective_q_min_mvar(self) -> float:
        return max(self.q_min_mvar, -self.s_max_mva)

    @property
    def effective_q_max_mvar(self) -> float:
        return min(self.q_max_mvar, self.s_max_mva)
