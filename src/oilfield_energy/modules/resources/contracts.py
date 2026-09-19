"""Immutable device identity and zero-active-power SVG capability contracts."""

from dataclasses import dataclass
from math import isfinite
from typing import Literal

ResourceKind = Literal["wind", "pv", "storage", "svg"]


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
