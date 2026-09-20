"""参数化模拟算例。

所有数据均为研究用途的模拟值，不代表长庆油田真实设备或运行数据。
后续取得真实数据后，仅需替换本模块产生的 :class:`ProjectCase`。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Dict, List

import numpy as np

from .modules.resources.contracts import ResourceIdentity, ResourceKind, SvgCapability

if TYPE_CHECKING:
    from .network_model import NetworkModelV2


@dataclass(frozen=True)
class Line:
    name: str
    parent: str
    child: str
    r_pu: float
    x_pu: float
    s_max_mva: float


@dataclass(frozen=True)
class Storage:
    bus: str
    p_max_mw: float
    e_max_mwh: float
    e_min_mwh: float
    e_initial_mwh: float
    eta_charge: float
    eta_discharge: float
    s_max_mva: float


@dataclass(frozen=True)
class MicrogridData:
    name: str
    buses: List[str]
    lines: List[Line]
    pcc_bus: str
    base_mva: float
    maximum_load_mw: float
    p_grid_max_mw: float
    p_grid_min_mw: float
    voltage_min_pu: float
    voltage_max_pu: float
    load_p_mw: Dict[str, np.ndarray]
    load_q_mvar: Dict[str, np.ndarray]
    wind_available_mw: Dict[str, np.ndarray]
    wind_capacity_mw: Dict[str, float]
    wind_capacity_mva: Dict[str, float]
    pv_available_mw: Dict[str, np.ndarray]
    pv_capacity_mw: Dict[str, float]
    pv_capacity_mva: Dict[str, float]
    storage: Storage
    svg_bus: str
    svg_q_min_mvar: float
    svg_q_max_mvar: float
    # 现场能力口径。None 表示合成/旧算例只使用变流器视在容量包络；
    # 真实算例必须显式给出。逐母线开关避免把“有逆变器”误当成“获准调Q”。
    wind_q_abs_over_p_max: Dict[str, float] | None = None
    pv_reactive_enabled: Dict[str, bool] | None = None
    storage_reactive_enabled: bool = True
    # NetworkModelV2 是网络台账的权威契约；``lines`` 暂时保留为当前求解器的
    # 兼容视图。求解前必须校验两者完全一致，不能绕过 V2 使用旧线路列表。
    network_model_v2: NetworkModelV2 | None = None
    network_operating_mode_id: str | None = None
    resource_identities: tuple[ResourceIdentity, ...] = ()
    svg_s_max_mva: float | None = None

    def __post_init__(self) -> None:
        if self.resource_identities:
            expected = (
                {("wind", bus) for bus in self.wind_available_mw}
                | {("pv", bus) for bus in self.pv_available_mw}
                | {("storage", self.storage.bus), ("svg", self.svg_bus)}
            )
            keys = {(r.kind, r.bus_id) for r in self.resource_identities}
            ids = {r.resource_id for r in self.resource_identities}
            if (
                keys != expected
                or len(keys) != len(self.resource_identities)
                or len(ids) != len(keys)
            ):
                raise ValueError(
                    "resource identities must cover each modeled resource exactly once"
                )
        self.svg_capability()

    def resource_id(self, kind: ResourceKind, bus: str) -> str:
        if self.resource_identities:
            for resource in self.resource_identities:
                if (resource.kind, resource.bus_id) == (kind, bus):
                    return resource.resource_id
            raise ValueError(f"missing resource identity: {kind}:{bus}")
        return f"{self.name}:{kind}:{bus}"

    def svg_capability(self) -> SvgCapability:
        # Legacy synthetic cases declared only Q; retain their original envelope.
        apparent = self.svg_s_max_mva
        if apparent is None:
            apparent = max(abs(self.svg_q_min_mvar), abs(self.svg_q_max_mvar), 1e-12)
        return SvgCapability(self.svg_q_min_mvar, self.svg_q_max_mvar, apparent)

    def wind_q_over_p_limit(self, bus: str) -> float | None:
        if self.wind_q_abs_over_p_max is None:
            return None
        return self.wind_q_abs_over_p_max.get(bus)

    def pv_can_control_reactive(self, bus: str) -> bool:
        if self.pv_reactive_enabled is None:
            return True
        return bool(self.pv_reactive_enabled.get(bus, False))


@dataclass(frozen=True)
class ModelAssumptions:
    dt_hours: float = 0.25
    pf_min: float = 0.90
    pf_dispatch_target: float = 0.92
    no_reverse_margin_mw: float = 0.15
    terminal_energy_tolerance_mwh: float = 0.05
    curtailment_cost_cny_per_mwh: float = 520.0
    storage_degradation_cny_per_mwh: float = 70.0
    loss_value_cny_per_mwh: float = 650.0
    voltage_deviation_cost_cny_per_pu2h: float = 50.0
    local_import_ramp_cost_cny_per_mw: float = 12.0
    cluster_peak_cost_cny_per_mw: float = 220.0
    cluster_ramp_cost_cny_per_mw: float = 18.0
    polygon_sides: int = 16
    square_tangent_points: int = 7


@dataclass(frozen=True)
class ProjectCase:
    time_hours: np.ndarray
    price_cny_per_mwh: np.ndarray
    microgrids: List[MicrogridData]
    assumptions: ModelAssumptions
    cluster_import_limit_mw: float
    dataset_id: str | None = None
    dataset_revision: str | None = None
    dataset_sha256: str | None = None
    profile_kind: str | None = None


def renewable_active_power_limits(
    available_mw: Dict[str, np.ndarray],
    rated_capacity_mw: Dict[str, float],
    time_steps: int,
    *,
    label: str,
) -> Dict[str, np.ndarray]:
    """校验风光有功输入并按MW额定值限幅；不修改原始预测。

    MVA变流器容量不能替代MW额定容量。高于额定值的预测允许保留作诊断，
    但可调度上界不得超过额定值；缺失、负值或非有限输入必须明确拒绝。
    """
    limits: Dict[str, np.ndarray] = {}
    for bus, available in available_mw.items():
        if bus not in rated_capacity_mw:
            raise ValueError(f"{label}:{bus} requires rated active capacity in MW")
        rated = float(rated_capacity_mw[bus])
        if not np.isfinite(rated) or rated < 0.0:
            raise ValueError(f"{label}:{bus} rated MW capacity must be finite and nonnegative")
        profile = np.asarray(available, dtype=float)
        if profile.shape != (time_steps,):
            raise ValueError(f"{label}:{bus} availability must contain {time_steps} time steps")
        if not np.all(np.isfinite(profile)) or np.any(profile < 0.0):
            raise ValueError(f"{label}:{bus} availability must be finite and nonnegative")
        limits[bus] = np.minimum(profile, rated)
    return limits
