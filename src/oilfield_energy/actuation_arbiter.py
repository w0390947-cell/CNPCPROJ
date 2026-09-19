"""群控、硬保护和设备能力之间的唯一绝对有功限额仲裁器。"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from math import isfinite

from .control_contracts import (
    ArbitratedStationCap,
    CapIntentAction,
    StationCapIntent,
)


class ActuationArbiter:
    """按控制所有权锁存绝对 cap，并只产生一个逐站有效上限。

    ``NO_CHANGE``保留 owner 现有限额，``RELEASE_CAP``明确释放该 owner 的约束；
    因而不会把“本周期不写”和“解除限发”混为一谈。
    """

    def __init__(self) -> None:
        self._caps: dict[str, dict[str, float]] = {}

    def reset(self) -> None:
        self._caps.clear()

    def owner_cap(self, owner_id: str, station_id: str) -> float | None:
        return self._caps.get(owner_id, {}).get(station_id)

    def owner_caps(self, owner_id: str) -> dict[str, float]:
        return dict(self._caps.get(owner_id, {}))

    def apply(
        self,
        device_available_mw: Mapping[str, float],
        intents: Iterable[StationCapIntent] = (),
    ) -> tuple[ArbitratedStationCap, ...]:
        available = self._validated_available(device_available_mw)
        batch = tuple(intents)
        keys = [(intent.owner_id, intent.station_id) for intent in batch]
        if len(set(keys)) != len(keys):
            raise ValueError("an owner may submit at most one intent per station in a batch")
        unknown = sorted({intent.station_id for intent in batch}.difference(available))
        if unknown:
            raise KeyError(f"cap intents reference unknown stations: {unknown}")

        for intent in batch:
            owner_caps = self._caps.setdefault(intent.owner_id, {})
            if intent.action is CapIntentAction.SET_CAP:
                owner_caps[intent.station_id] = float(intent.absolute_cap_mw)
            elif intent.action is CapIntentAction.RELEASE_CAP:
                owner_caps.pop(intent.station_id, None)
            elif intent.action is not CapIntentAction.NO_CHANGE:
                raise ValueError(f"unsupported cap intent action: {intent.action}")
            if not owner_caps:
                self._caps.pop(intent.owner_id, None)

        return self.resolve(available)

    def resolve(
        self,
        device_available_mw: Mapping[str, float],
        *,
        excluded_owner_ids: Iterable[str] = (),
    ) -> tuple[ArbitratedStationCap, ...]:
        """只读解析当前绝对上限，可用于释放某一 owner 前的反事实安全检查。"""
        available = self._validated_available(device_available_mw)
        excluded = frozenset(excluded_owner_ids)
        results = []
        for station_id, device_cap in available.items():
            owner_values = tuple(sorted(
                (owner_id, caps[station_id])
                for owner_id, caps in self._caps.items()
                if owner_id not in excluded and station_id in caps
            ))
            effective = min(
                [float(device_cap), *(cap for _, cap in owner_values)]
            )
            results.append(ArbitratedStationCap(
                station_id=station_id,
                device_available_mw=float(device_cap),
                effective_cap_mw=effective,
                owner_caps=owner_values,
            ))
        return tuple(results)

    @staticmethod
    def _validated_available(
        device_available_mw: Mapping[str, float],
    ) -> dict[str, float]:
        available = dict(device_available_mw)
        for station_id, value in available.items():
            if not isinstance(station_id, str) or not station_id.strip():
                raise ValueError("device station ids must be non-empty strings")
            if not isinstance(value, (int, float)) or not isfinite(float(value)) or value < 0.0:
                raise ValueError("device available caps must be nonnegative and finite")
        return {station_id: float(value) for station_id, value in available.items()}


__all__ = ["ActuationArbiter"]
