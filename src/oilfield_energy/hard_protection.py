"""硬防倒送上限的释放锁存。"""

from __future__ import annotations

from dataclasses import dataclass
from math import isfinite
from numbers import Integral


@dataclass
class HardCapReleaseGuard:
    """仅在反事实PCC连续处于释放阈值之上时允许解锁。"""

    hysteresis_mw: float
    confirmation_cycles: int
    confirmation_count: int = 0

    def __post_init__(self) -> None:
        if not isfinite(self.hysteresis_mw) or self.hysteresis_mw < 0.0:
            raise ValueError("hysteresis_mw must be nonnegative and finite")
        if (
            isinstance(self.confirmation_cycles, bool)
            or not isinstance(self.confirmation_cycles, Integral)
            or self.confirmation_cycles <= 0
        ):
            raise ValueError("confirmation_cycles must be positive")

    def reset(self) -> None:
        self.confirmation_count = 0

    def observe(self, counterfactual_pcc_mw: float, protection_margin_mw: float) -> bool:
        if not isfinite(counterfactual_pcc_mw) or not isfinite(protection_margin_mw):
            self.reset()
            return False
        release_floor = protection_margin_mw + self.hysteresis_mw
        if counterfactual_pcc_mw >= release_floor:
            self.confirmation_count += 1
        else:
            self.reset()
        return self.confirmation_count >= self.confirmation_cycles


__all__ = ["HardCapReleaseGuard"]
