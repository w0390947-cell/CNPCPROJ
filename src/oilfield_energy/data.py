"""参数化模拟算例。

所有数据均为研究用途的模拟值，不代表长庆油田真实设备或运行数据。
后续取得真实数据后，仅需替换本模块产生的 :class:`ProjectCase`。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Dict, List

import numpy as np

from .network_model import build_legacy_network_model_v2
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


def _daily_profiles(time_hours: np.ndarray, scale: float, phase: float) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """生成平滑且确定性的负荷、风电、光伏标幺曲线。"""
    h = time_hours
    morning = 0.16 * np.exp(-0.5 * ((h - 9.0) / 2.2) ** 2)
    evening = 0.28 * np.exp(-0.5 * ((h - 19.0) / 2.7) ** 2)
    production = 0.72 + morning + evening + 0.025 * np.sin(2 * np.pi * (h + phase) / 24)
    load = scale * production

    pv = np.maximum(0.0, np.sin(np.pi * (h - 6.5) / 12.0)) ** 1.7
    cloud = 1.0 - 0.18 * np.exp(-0.5 * ((h - 13.5 - 0.2 * phase) / 0.9) ** 2)
    pv *= cloud

    wind = (
        0.50
        + 0.20 * np.sin(2 * np.pi * (h + 2.5 * phase) / 24)
        + 0.10 * np.sin(2 * np.pi * (h + phase) / 7.0)
    )
    wind = np.clip(wind, 0.18, 0.86)
    return load, wind, pv


def _price_profile(time_hours: np.ndarray) -> np.ndarray:
    price = np.full_like(time_hours, 560.0, dtype=float)
    price[(time_hours >= 0) & (time_hours < 7)] = 280.0
    price[(time_hours >= 7) & (time_hours < 10)] = 720.0
    price[(time_hours >= 10) & (time_hours < 12)] = 930.0
    price[(time_hours >= 12) & (time_hours < 17)] = 610.0
    price[(time_hours >= 17) & (time_hours < 22)] = 1_180.0
    price[(time_hours >= 22)] = 430.0
    return price


def _make_microgrid(
    name: str,
    time_hours: np.ndarray,
    load_scale: float,
    wind_mw: float,
    pv_mw: float,
    storage_power_mw: float,
    storage_energy_mwh: float,
    phase: float,
) -> MicrogridData:
    buses = [f"{name}_PCC", f"{name}_MAIN", f"{name}_WIND", f"{name}_PV", f"{name}_FLEX"]
    pcc, main, wind_bus, pv_bus, flex_bus = buses
    load, wind_cf, pv_cf = _daily_profiles(time_hours, load_scale, phase)
    shares = {main: 0.38, wind_bus: 0.20, pv_bus: 0.24, flex_bus: 0.18}
    load_p = {bus: load * share for bus, share in shares.items()}
    load_p[pcc] = np.zeros_like(load)
    # 油田电机负荷的基础功率因数取 0.92（滞后）。
    q_ratio = np.tan(np.arccos(0.92))
    load_q = {bus: values * q_ratio for bus, values in load_p.items()}

    lines = [
        Line(f"{name}_L01", pcc, main, 0.0045, 0.0090, 18.0),
        Line(f"{name}_L12", main, wind_bus, 0.0100, 0.0140, 11.0),
        Line(f"{name}_L13", main, pv_bus, 0.0120, 0.0150, 8.0),
        Line(f"{name}_L14", main, flex_bus, 0.0090, 0.0130, 7.0),
    ]
    wind_available = {wind_bus: wind_mw * wind_cf}
    pv_available = {pv_bus: pv_mw * pv_cf}
    storage = Storage(
        bus=flex_bus,
        p_max_mw=storage_power_mw,
        e_max_mwh=storage_energy_mwh,
        e_min_mwh=0.10 * storage_energy_mwh,
        e_initial_mwh=0.50 * storage_energy_mwh,
        eta_charge=0.95,
        eta_discharge=0.95,
        s_max_mva=1.08 * storage_power_mw,
    )
    network_model_v2 = build_legacy_network_model_v2(
        network_id=f"{name}-synthetic-network",
        buses=buses,
        pcc_bus_id=pcc,
        base_mva=20.0,
        lines=lines,
    )
    return MicrogridData(
        name=name,
        buses=buses,
        lines=lines,
        pcc_bus=pcc,
        base_mva=20.0,
        maximum_load_mw=1.20 * load_scale,
        p_grid_max_mw=18.0,
        p_grid_min_mw=0.15,
        voltage_min_pu=0.95,
        voltage_max_pu=1.05,
        load_p_mw=load_p,
        load_q_mvar=load_q,
        wind_available_mw=wind_available,
        wind_capacity_mw={wind_bus: wind_mw},
        wind_capacity_mva={wind_bus: 1.05 * wind_mw},
        pv_available_mw=pv_available,
        pv_capacity_mw={pv_bus: pv_mw},
        pv_capacity_mva={pv_bus: 1.05 * pv_mw},
        storage=storage,
        svg_bus=flex_bus,
        svg_q_min_mvar=-2.5,
        svg_q_max_mvar=2.5,
        network_model_v2=network_model_v2,
        network_operating_mode_id=network_model_v2.default_operating_mode_id,
    )


def build_synthetic_case(steps: int = 96) -> ProjectCase:
    """构造山城型单微网和三个跨区域微网的统一模拟算例。

    ``steps=96`` 对应 24 小时、15 分钟分辨率。测试时可降低步数，但
    时间跨度始终为完整一天。
    """
    if steps <= 0:
        raise ValueError("steps must be positive")
    dt = 24.0 / steps
    time_hours = np.arange(steps, dtype=float) * dt
    assumptions = ModelAssumptions(dt_hours=dt)

    shancheng = _make_microgrid(
        "SC", time_hours, load_scale=9.8, wind_mw=10.0, pv_mw=4.2,
        storage_power_mw=2.5, storage_energy_mwh=5.0, phase=0.0,
    )
    yan_an_b = _make_microgrid(
        "YA_B", time_hours, load_scale=8.0, wind_mw=7.0, pv_mw=3.2,
        storage_power_mw=2.0, storage_energy_mwh=4.0, phase=0.8,
    )
    yan_an_c = _make_microgrid(
        "YA_C", time_hours, load_scale=7.2, wind_mw=6.0, pv_mw=3.8,
        storage_power_mw=1.8, storage_energy_mwh=3.6, phase=1.6,
    )
    # 三个微网共同受上级电网通道容量约束，形成集群层耦合。
    cluster_limit = 26.0
    return ProjectCase(
        time_hours=time_hours,
        price_cny_per_mwh=_price_profile(time_hours),
        microgrids=[shancheng, yan_an_b, yan_an_c],
        assumptions=assumptions,
        cluster_import_limit_mw=cluster_limit,
    )
