"""基于 LinDistFlow 的混合整数线性优化模型。

求解器使用 :func:`scipy.optimize.milp`，其后端为开源 HiGHS。模型采用：

* 径向配电网 LinDistFlow 电压方程；
* 多切线分段线性近似线路有功/无功平方，从而计入网损；
* 多边形内逼近视在功率圆；
* 二进制变量禁止储能同时充放电；
* 各 PCC 防倒送和功率因数硬约束；
* 集群总受电能力、峰值和爬坡耦合。
"""

from __future__ import annotations

from dataclasses import dataclass
from math import acos, cos, pi, sin, tan
from typing import Dict, Hashable, Iterable, List, Mapping, Sequence

import numpy as np
from scipy.optimize import Bounds, LinearConstraint, milp
from scipy.sparse import coo_matrix

from .data import MicrogridData, ProjectCase, renewable_active_power_limits
from .modules.dispatch.api import account_renewable, evaluate_economics
from .modules.dispatch.contracts import ACCOUNTING_VERSION, CostRates
from .network_model import validate_legacy_network_alignment
from .resource_control_contracts import (
    ResourceSchedule,
    ResourceType,
    validate_resource_schedules,
)

INF = np.inf


class _LinearModel:
    def __init__(self) -> None:
        self.names: List[str] = []
        self.c: List[float] = []
        self.lb: List[float] = []
        self.ub: List[float] = []
        self.integrality: List[int] = []
        self.rows: List[Dict[int, float]] = []
        self.row_lb: List[float] = []
        self.row_ub: List[float] = []
        self.row_names: List[str] = []

    def var(
        self,
        name: str,
        lb: float = -INF,
        ub: float = INF,
        objective: float = 0.0,
        integer: bool = False,
    ) -> int:
        idx = len(self.names)
        self.names.append(name)
        self.c.append(float(objective))
        self.lb.append(float(lb))
        self.ub.append(float(ub))
        self.integrality.append(1 if integer else 0)
        return idx

    def constraint(
        self,
        terms: Mapping[int, float],
        lb: float = -INF,
        ub: float = INF,
        name: str = "",
    ) -> None:
        cleaned = {idx: float(value) for idx, value in terms.items() if abs(value) > 1e-12}
        self.rows.append(cleaned)
        self.row_lb.append(float(lb))
        self.row_ub.append(float(ub))
        self.row_names.append(name)

    def solve(self, time_limit_seconds: float = 180.0, mip_gap: float = 1e-4):
        row_idx: List[int] = []
        col_idx: List[int] = []
        values: List[float] = []
        for r, terms in enumerate(self.rows):
            for c, value in terms.items():
                row_idx.append(r)
                col_idx.append(c)
                values.append(value)
        matrix = coo_matrix(
            (values, (row_idx, col_idx)),
            shape=(len(self.rows), len(self.names)),
            dtype=float,
        ).tocsr()
        return milp(
            c=np.asarray(self.c),
            integrality=np.asarray(self.integrality),
            bounds=Bounds(np.asarray(self.lb), np.asarray(self.ub)),
            constraints=LinearConstraint(matrix, np.asarray(self.row_lb), np.asarray(self.row_ub)),
            options={
                "disp": False,
                "time_limit": time_limit_seconds,
                "mip_rel_gap": mip_gap,
                "presolve": True,
            },
        )


@dataclass
class OptimizationResult:
    success: bool
    status: int
    message: str
    objective_cny: float
    solver_objective_without_constants_cny: float
    mip_gap: float | None
    microgrids: Dict[str, Dict[str, object]]
    cluster: Dict[str, object]
    model_size: Dict[str, int]


def _add(terms: Dict[int, float], idx: int, value: float) -> None:
    terms[idx] = terms.get(idx, 0.0) + value


def _polygon_constraints(
    model: _LinearModel,
    p_terms: Mapping[int, float],
    q_terms: Mapping[int, float],
    s_max: float,
    sides: int,
    name: str,
) -> None:
    # rhs 使用 cos(pi/n)，确保多边形位于容量圆内部（保守约束）。
    rhs = s_max * cos(pi / sides)
    for k in range(sides):
        angle = 2.0 * pi * k / sides
        terms: Dict[int, float] = {}
        for idx, coef in p_terms.items():
            _add(terms, idx, cos(angle) * coef)
        for idx, coef in q_terms.items():
            _add(terms, idx, sin(angle) * coef)
        model.constraint(terms, ub=rhs, name=f"{name}_poly_{k}")


def _selected_microgrids(case: ProjectCase, names: Sequence[str] | None) -> List[MicrogridData]:
    if names is None:
        return list(case.microgrids)
    lookup = {mg.name: mg for mg in case.microgrids}
    missing = [name for name in names if name not in lookup]
    if missing:
        raise KeyError(f"unknown microgrids: {missing}")
    return [lookup[name] for name in names]


def solve_case(
    case: ProjectCase,
    microgrid_names: Sequence[str] | None = None,
    *,
    storage_enabled: bool = True,
    cluster_coordination: bool = True,
    pcc_targets: Mapping[str, Mapping[str, np.ndarray]] | None = None,
    p_tracking_penalty_cny_per_mw: float = 20_000.0,
    q_tracking_penalty_cny_per_mvar: float = 8_000.0,
    time_limit_seconds: float = 180.0,
) -> OptimizationResult:
    """建立并求解单微网或多微网协同模型。"""
    selected = _selected_microgrids(case, microgrid_names)
    if not selected:
        raise ValueError("at least one microgrid is required")
    networks = {
        microgrid.name: validate_legacy_network_alignment(microgrid)
        for microgrid in selected
    }
    T = len(case.time_hours)
    a = case.assumptions
    model = _LinearModel()
    refs: Dict[str, Dict[str, Dict[Hashable, int]]] = {}
    curtailment_constant = 0.0

    for mg in selected:
        network = networks[mg.name]
        lines = network.branches
        shunt_q_nominal_by_bus = network.shunt_q_nominal_mvar_by_bus
        wind_limits = renewable_active_power_limits(
            mg.wind_available_mw, mg.wind_capacity_mw, T, label=f"{mg.name}:wind",
        )
        pv_limits = renewable_active_power_limits(
            mg.pv_available_mw, mg.pv_capacity_mw, T, label=f"{mg.name}:pv",
        )
        r: Dict[str, Dict[Hashable, int]] = {
            "p_grid": {}, "q_grid": {}, "v": {}, "vdev": {},
            "p_grid_ramp": {},
            "p_target_dev": {}, "q_target_dev": {},
            "p_line": {}, "q_line": {}, "p2": {}, "q2": {}, "loss": {},
            "p_wind": {}, "q_wind": {}, "p_pv": {}, "q_pv": {},
            "p_ch": {}, "p_dis": {}, "q_ess": {}, "charge_mode": {}, "energy": {},
            "q_svg": {},
        }
        refs[mg.name] = r
        target = pcc_targets.get(mg.name) if pcc_targets is not None else None
        if target is not None:
            if "p_mw" not in target or "q_mvar" not in target:
                raise KeyError(f"{mg.name} target requires p_mw and q_mvar")
            if len(target["p_mw"]) != T or len(target["q_mvar"]) != T:
                raise ValueError(f"{mg.name} target length must equal {T}")
        p_min = max(mg.p_grid_min_mw, a.no_reverse_margin_mw)
        # 优化层采用更严格的运行目标，为线性模型到 AC 校核的偏差留裕度。
        pf_tan = tan(acos(a.pf_dispatch_target))

        for t in range(T):
            r["p_grid"][t] = model.var(
                f"{mg.name}.p_grid[{t}]", p_min, mg.p_grid_max_mw,
                objective=case.price_cny_per_mwh[t] * a.dt_hours,
            )
            r["q_grid"][t] = model.var(f"{mg.name}.q_grid[{t}]", -mg.p_grid_max_mw, mg.p_grid_max_mw)
            model.constraint(
                {r["q_grid"][t]: 1.0, r["p_grid"][t]: -pf_tan},
                ub=-pf_tan * p_min, name=f"{mg.name}.pf_pos[{t}]",
            )
            model.constraint(
                {r["q_grid"][t]: -1.0, r["p_grid"][t]: -pf_tan},
                ub=-pf_tan * p_min, name=f"{mg.name}.pf_neg[{t}]",
            )
            if target is not None:
                r["p_target_dev"][t] = model.var(
                    f"{mg.name}.p_target_dev[{t}]", 0.0, INF,
                    objective=p_tracking_penalty_cny_per_mw * a.dt_hours,
                )
                r["q_target_dev"][t] = model.var(
                    f"{mg.name}.q_target_dev[{t}]", 0.0, INF,
                    objective=q_tracking_penalty_cny_per_mvar * a.dt_hours,
                )
                p_ref = float(target["p_mw"][t])
                q_ref = float(target["q_mvar"][t])
                model.constraint(
                    {r["p_grid"][t]: 1.0, r["p_target_dev"][t]: -1.0},
                    ub=p_ref, name=f"{mg.name}.p_target_up[{t}]",
                )
                model.constraint(
                    {r["p_grid"][t]: -1.0, r["p_target_dev"][t]: -1.0},
                    ub=-p_ref, name=f"{mg.name}.p_target_down[{t}]",
                )
                model.constraint(
                    {r["q_grid"][t]: 1.0, r["q_target_dev"][t]: -1.0},
                    ub=q_ref, name=f"{mg.name}.q_target_up[{t}]",
                )
                model.constraint(
                    {r["q_grid"][t]: -1.0, r["q_target_dev"][t]: -1.0},
                    ub=-q_ref, name=f"{mg.name}.q_target_down[{t}]",
                )

            for bus in mg.buses:
                if bus == mg.pcc_bus:
                    lo = hi = 1.0
                else:
                    lo = mg.voltage_min_pu ** 2
                    hi = mg.voltage_max_pu ** 2
                r["v"][(bus, t)] = model.var(f"{mg.name}.v2[{bus},{t}]", lo, hi)
                r["vdev"][(bus, t)] = model.var(
                    f"{mg.name}.vdev[{bus},{t}]", 0.0, INF,
                    objective=a.voltage_deviation_cost_cny_per_pu2h * a.dt_hours,
                )
                model.constraint(
                    {r["v"][(bus, t)]: 1.0, r["vdev"][(bus, t)]: -1.0},
                    ub=1.0, name=f"{mg.name}.vdev_up[{bus},{t}]",
                )
                model.constraint(
                    {r["v"][(bus, t)]: -1.0, r["vdev"][(bus, t)]: -1.0},
                    ub=-1.0, name=f"{mg.name}.vdev_down[{bus},{t}]",
                )

            for line in lines:
                key = (line.name, t)
                s = line.s_max_mva
                r["p_line"][key] = model.var(f"{mg.name}.p_line[{line.name},{t}]", -s, s)
                r["q_line"][key] = model.var(f"{mg.name}.q_line[{line.name},{t}]", -s, s)
                r["p2"][key] = model.var(f"{mg.name}.p2[{line.name},{t}]", 0.0, s * s)
                r["q2"][key] = model.var(f"{mg.name}.q2[{line.name},{t}]", 0.0, s * s)
                r["loss"][key] = model.var(
                    f"{mg.name}.loss[{line.name},{t}]", 0.0, INF,
                    objective=a.loss_value_cny_per_mwh * a.dt_hours,
                )
                _polygon_constraints(
                    model, {r["p_line"][key]: 1.0}, {r["q_line"][key]: 1.0},
                    s, a.polygon_sides, f"{mg.name}.{line.name}[{t}]",
                )
                # 旧LinDistFlow只建模有功损耗，接收端P/Q须按其平衡方程计算。
                _polygon_constraints(
                    model,
                    {r["p_line"][key]: 1.0, r["loss"][key]: -1.0},
                    {r["q_line"][key]: 1.0},
                    s, a.polygon_sides, f"{mg.name}.{line.name}.receiving[{t}]",
                )
                tangent_points = np.linspace(-s, s, a.square_tangent_points)
                for x0 in tangent_points:
                    # z >= 2*x0*x - x0^2
                    model.constraint(
                        {r["p_line"][key]: 2.0 * x0, r["p2"][key]: -1.0},
                        ub=x0 * x0, name=f"{mg.name}.p2_tan[{line.name},{t},{x0:.3f}]",
                    )
                    model.constraint(
                        {r["q_line"][key]: 2.0 * x0, r["q2"][key]: -1.0},
                        ub=x0 * x0, name=f"{mg.name}.q2_tan[{line.name},{t},{x0:.3f}]",
                    )
                model.constraint(
                    {
                        r["loss"][key]: 1.0,
                        r["p2"][key]: -line.r_pu * line.tap_ratio ** 2 / mg.base_mva,
                        r["q2"][key]: -line.r_pu * line.tap_ratio ** 2 / mg.base_mva,
                    },
                    lb=0.0, ub=0.0, name=f"{mg.name}.loss_def[{line.name},{t}]",
                )
                model.constraint(
                    {
                        r["v"][(line.child, t)]: 1.0,
                        r["v"][(line.parent, t)]: -1.0 / line.tap_ratio ** 2,
                        r["p_line"][key]: 2.0 * line.r_pu / mg.base_mva,
                        r["q_line"][key]: 2.0 * line.x_pu / mg.base_mva,
                    },
                    lb=0.0, ub=0.0, name=f"{mg.name}.voltage_drop[{line.name},{t}]",
                )

            for bus, available in mg.wind_available_mw.items():
                key = (bus, t)
                cap = mg.wind_capacity_mva[bus]
                r["p_wind"][key] = model.var(
                    f"{mg.name}.p_wind[{bus},{t}]", 0.0, float(wind_limits[bus][t]),
                    objective=-a.curtailment_cost_cny_per_mwh * a.dt_hours,
                )
                r["q_wind"][key] = model.var(f"{mg.name}.q_wind[{bus},{t}]", -cap, cap)
                _polygon_constraints(
                    model, {r["p_wind"][key]: 1.0}, {r["q_wind"][key]: 1.0},
                    cap, a.polygon_sides, f"{mg.name}.wind[{bus},{t}]",
                )
                q_over_p = mg.wind_q_over_p_limit(bus)
                if q_over_p is not None:
                    if q_over_p < 0.0:
                        raise ValueError("wind Q/P capability ratio must be nonnegative")
                    model.constraint(
                        {r["q_wind"][key]: 1.0, r["p_wind"][key]: -q_over_p},
                        ub=0.0, name=f"{mg.name}.wind_qp_pos[{bus},{t}]",
                    )
                    model.constraint(
                        {r["q_wind"][key]: -1.0, r["p_wind"][key]: -q_over_p},
                        ub=0.0, name=f"{mg.name}.wind_qp_neg[{bus},{t}]",
                    )
                curtailment_constant += (
                    a.curtailment_cost_cny_per_mwh * float(wind_limits[bus][t]) * a.dt_hours
                )

            for bus, available in mg.pv_available_mw.items():
                key = (bus, t)
                cap = mg.pv_capacity_mva[bus]
                r["p_pv"][key] = model.var(
                    f"{mg.name}.p_pv[{bus},{t}]", 0.0, float(pv_limits[bus][t]),
                    objective=-a.curtailment_cost_cny_per_mwh * a.dt_hours,
                )
                pv_q_cap = cap if mg.pv_can_control_reactive(bus) else 0.0
                r["q_pv"][key] = model.var(
                    f"{mg.name}.q_pv[{bus},{t}]", -pv_q_cap, pv_q_cap
                )
                _polygon_constraints(
                    model, {r["p_pv"][key]: 1.0}, {r["q_pv"][key]: 1.0},
                    cap, a.polygon_sides, f"{mg.name}.pv[{bus},{t}]",
                )
                curtailment_constant += (
                    a.curtailment_cost_cny_per_mwh * float(pv_limits[bus][t]) * a.dt_hours
                )

            st = mg.storage
            pmax = st.p_max_mw if storage_enabled else 0.0
            qmax = (
                st.s_max_mva
                if storage_enabled and mg.storage_reactive_enabled
                else 0.0
            )
            r["p_ch"][t] = model.var(
                f"{mg.name}.p_ch[{t}]", 0.0, pmax,
                objective=a.storage_degradation_cny_per_mwh * a.dt_hours,
            )
            r["p_dis"][t] = model.var(
                f"{mg.name}.p_dis[{t}]", 0.0, pmax,
                objective=a.storage_degradation_cny_per_mwh * a.dt_hours,
            )
            r["q_ess"][t] = model.var(f"{mg.name}.q_ess[{t}]", -qmax, qmax)
            r["charge_mode"][t] = model.var(
                f"{mg.name}.charge_mode[{t}]", 0.0, 1.0,
                integer=storage_enabled,
            )
            if storage_enabled:
                model.constraint(
                    {r["p_ch"][t]: 1.0, r["charge_mode"][t]: -pmax},
                    ub=0.0, name=f"{mg.name}.charge_gate[{t}]",
                )
                model.constraint(
                    {r["p_dis"][t]: 1.0, r["charge_mode"][t]: pmax},
                    ub=pmax, name=f"{mg.name}.discharge_gate[{t}]",
                )
                _polygon_constraints(
                    model,
                    {r["p_dis"][t]: 1.0, r["p_ch"][t]: -1.0},
                    {r["q_ess"][t]: 1.0}, st.s_max_mva, a.polygon_sides,
                    f"{mg.name}.ess[{t}]",
                )
            else:
                model.constraint({r["charge_mode"][t]: 1.0}, lb=0.0, ub=0.0, name=f"{mg.name}.mode_off[{t}]")

            r["q_svg"][t] = model.var(
                f"{mg.name}.q_svg[{t}]",
                mg.svg_capability().effective_q_min_mvar,
                mg.svg_capability().effective_q_max_mvar,
            )

        st = mg.storage
        for t in range(1, T):
            ramp = model.var(
                f"{mg.name}.p_grid_ramp[{t}]", 0.0, INF,
                objective=a.local_import_ramp_cost_cny_per_mw,
            )
            r["p_grid_ramp"][t] = ramp
            model.constraint(
                {r["p_grid"][t]: 1.0, r["p_grid"][t - 1]: -1.0, ramp: -1.0},
                ub=0.0, name=f"{mg.name}.local_ramp_up[{t}]",
            )
            model.constraint(
                {r["p_grid"][t]: -1.0, r["p_grid"][t - 1]: 1.0, ramp: -1.0},
                ub=0.0, name=f"{mg.name}.local_ramp_down[{t}]",
            )

        for t in range(T + 1):
            if not storage_enabled:
                lo = hi = st.e_initial_mwh
            elif t == 0:
                lo = hi = st.e_initial_mwh
            elif t == T:
                lo = max(st.e_min_mwh, st.e_initial_mwh - a.terminal_energy_tolerance_mwh)
                hi = min(st.e_max_mwh, st.e_initial_mwh + a.terminal_energy_tolerance_mwh)
            else:
                lo, hi = st.e_min_mwh, st.e_max_mwh
            r["energy"][t] = model.var(f"{mg.name}.energy[{t}]", lo, hi)
        for t in range(T):
            model.constraint(
                {
                    r["energy"][t + 1]: 1.0,
                    r["energy"][t]: -1.0,
                    r["p_ch"][t]: -st.eta_charge * a.dt_hours,
                    r["p_dis"][t]: a.dt_hours / st.eta_discharge,
                },
                lb=0.0, ub=0.0, name=f"{mg.name}.energy_balance[{t}]",
            )

        incoming = {bus: [] for bus in mg.buses}
        outgoing = {bus: [] for bus in mg.buses}
        for line in lines:
            incoming[line.child].append(line)
            outgoing[line.parent].append(line)

        for t in range(T):
            for bus in mg.buses:
                p_terms: Dict[int, float] = {}
                q_terms: Dict[int, float] = {}
                for line in incoming[bus]:
                    key = (line.name, t)
                    _add(p_terms, r["p_line"][key], 1.0)
                    _add(p_terms, r["loss"][key], -1.0)
                    _add(q_terms, r["q_line"][key], 1.0)
                for line in outgoing[bus]:
                    key = (line.name, t)
                    _add(p_terms, r["p_line"][key], -1.0)
                    _add(q_terms, r["q_line"][key], -1.0)
                if bus == mg.pcc_bus:
                    _add(p_terms, r["p_grid"][t], 1.0)
                    _add(q_terms, r["q_grid"][t], 1.0)
                if (bus, t) in r["p_wind"]:
                    _add(p_terms, r["p_wind"][(bus, t)], 1.0)
                    _add(q_terms, r["q_wind"][(bus, t)], 1.0)
                if (bus, t) in r["p_pv"]:
                    _add(p_terms, r["p_pv"][(bus, t)], 1.0)
                    _add(q_terms, r["q_pv"][(bus, t)], 1.0)
                if bus == mg.storage.bus:
                    _add(p_terms, r["p_dis"][t], 1.0)
                    _add(p_terms, r["p_ch"][t], -1.0)
                    _add(q_terms, r["q_ess"][t], 1.0)
                if bus == mg.svg_bus:
                    _add(q_terms, r["q_svg"][t], 1.0)
                _add(
                    q_terms,
                    r["v"][(bus, t)],
                    shunt_q_nominal_by_bus[bus],
                )
                model.constraint(
                    p_terms, lb=float(mg.load_p_mw[bus][t]), ub=float(mg.load_p_mw[bus][t]),
                    name=f"{mg.name}.p_balance[{bus},{t}]",
                )
                model.constraint(
                    q_terms,
                    lb=float(mg.load_q_mvar[bus][t]),
                    ub=float(mg.load_q_mvar[bus][t]),
                    name=f"{mg.name}.q_balance[{bus},{t}]",
                )

    cluster_refs: Dict[str, Dict[int, int] | int] = {"ramp": {}}
    if cluster_coordination and len(selected) > 1:
        peak = model.var("cluster.peak_import", 0.0, INF, objective=a.cluster_peak_cost_cny_per_mw)
        cluster_refs["peak"] = peak
        for t in range(T):
            total_terms = {refs[mg.name]["p_grid"][t]: 1.0 for mg in selected}
            model.constraint(total_terms, ub=case.cluster_import_limit_mw, name=f"cluster.import_limit[{t}]")
            peak_terms = dict(total_terms)
            _add(peak_terms, peak, -1.0)
            model.constraint(peak_terms, ub=0.0, name=f"cluster.peak[{t}]")
            if t > 0:
                ramp = model.var(
                    f"cluster.ramp[{t}]", 0.0, INF,
                    objective=a.cluster_ramp_cost_cny_per_mw,
                )
                cluster_refs["ramp"][t] = ramp  # type: ignore[index]
                up: Dict[int, float] = {ramp: -1.0}
                down: Dict[int, float] = {ramp: -1.0}
                for mg in selected:
                    _add(up, refs[mg.name]["p_grid"][t], 1.0)
                    _add(up, refs[mg.name]["p_grid"][t - 1], -1.0)
                    _add(down, refs[mg.name]["p_grid"][t], -1.0)
                    _add(down, refs[mg.name]["p_grid"][t - 1], 1.0)
                model.constraint(up, ub=0.0, name=f"cluster.ramp_up[{t}]")
                model.constraint(down, ub=0.0, name=f"cluster.ramp_down[{t}]")

    raw = model.solve(time_limit_seconds=time_limit_seconds)
    if raw.x is None:
        return OptimizationResult(
            success=False, status=int(raw.status), message=str(raw.message),
            objective_cny=float("nan"), solver_objective_without_constants_cny=float("nan"),
            mip_gap=None, microgrids={}, cluster={},
            model_size={"variables": len(model.names), "constraints": len(model.rows), "binary_variables": int(sum(model.integrality))},
        )

    x = np.asarray(raw.x)
    result_mg: Dict[str, Dict[str, object]] = {}
    for mg in selected:
        network = networks[mg.name]
        lines = network.branches
        target = pcc_targets.get(mg.name) if pcc_targets is not None else None
        r = refs[mg.name]

        def arr(block: str, keys: Iterable[Hashable]) -> np.ndarray:
            return np.asarray([x[r[block][key]] for key in keys], dtype=float)

        p_grid = arr("p_grid", range(T))
        q_grid = arr("q_grid", range(T))
        # Report the bounds actually used by this backend, not the MISOCP reserve policy.
        p_floor = np.asarray([model.lb[r["p_grid"][t]] for t in range(T)])
        wind_schedules = tuple(
            ResourceSchedule(
                resource_id=mg.resource_id("wind", bus),
                bus_id=bus,
                resource_type=ResourceType.WIND,
                active_power_mw=arr("p_wind", [(bus, t) for t in range(T)]),
                reactive_power_mvar=arr("q_wind", [(bus, t) for t in range(T)]),
            )
            for bus in mg.wind_available_mw
        )
        pv_schedules = tuple(
            ResourceSchedule(
                resource_id=mg.resource_id("pv", bus),
                bus_id=bus,
                resource_type=ResourceType.PV,
                active_power_mw=arr("p_pv", [(bus, t) for t in range(T)]),
                reactive_power_mvar=arr("q_pv", [(bus, t) for t in range(T)]),
            )
            for bus in mg.pv_available_mw
        )
        wind_active_by_bus = {
            schedule.bus_id: schedule.active_power_mw
            for schedule in wind_schedules
        }
        wind_reactive_by_bus = {
            schedule.bus_id: schedule.reactive_power_mvar
            for schedule in wind_schedules
        }
        pv_active_by_bus = {
            schedule.bus_id: schedule.active_power_mw
            for schedule in pv_schedules
        }
        pv_reactive_by_bus = {
            schedule.bus_id: schedule.reactive_power_mvar
            for schedule in pv_schedules
        }
        p_wind = (
            np.sum(np.vstack(tuple(wind_active_by_bus.values())), axis=0)
            if wind_active_by_bus else np.zeros(T)
        )
        q_wind = (
            np.sum(np.vstack(tuple(wind_reactive_by_bus.values())), axis=0)
            if wind_reactive_by_bus else np.zeros(T)
        )
        p_pv = (
            np.sum(np.vstack(tuple(pv_active_by_bus.values())), axis=0)
            if pv_active_by_bus else np.zeros(T)
        )
        q_pv = (
            np.sum(np.vstack(tuple(pv_reactive_by_bus.values())), axis=0)
            if pv_reactive_by_bus else np.zeros(T)
        )
        wind_available = (
            np.sum(np.vstack(tuple(mg.wind_available_mw.values())), axis=0)
            if mg.wind_available_mw else np.zeros(T)
        )
        pv_available = (
            np.sum(np.vstack(tuple(mg.pv_available_mw.values())), axis=0)
            if mg.pv_available_mw else np.zeros(T)
        )
        p_ch = arr("p_ch", range(T))
        p_dis = arr("p_dis", range(T))
        energy = arr("energy", range(T + 1))
        q_svg = arr("q_svg", range(T))
        q_ess = arr("q_ess", range(T))
        storage_active = p_dis - p_ch
        storage_schedule = ResourceSchedule(
            resource_id=mg.resource_id("storage", mg.storage.bus),
            bus_id=mg.storage.bus,
            resource_type=ResourceType.STORAGE,
            active_power_mw=storage_active,
            reactive_power_mvar=q_ess,
        )
        svg_schedule = ResourceSchedule(
            resource_id=mg.resource_id("svg", mg.svg_bus),
            bus_id=mg.svg_bus,
            resource_type=ResourceType.SVG,
            active_power_mw=np.zeros(T),
            reactive_power_mvar=q_svg,
        )
        resource_schedules = validate_resource_schedules(
            (*wind_schedules, *pv_schedules, storage_schedule, svg_schedule),
            expected_time_steps=T,
        )
        # 记录除上级电网外的母线净需求，供独立 AC 潮流校核使用。
        bus_p_demand = np.vstack([np.asarray(mg.load_p_mw[bus], dtype=float).copy() for bus in mg.buses])
        bus_q_demand = np.vstack([np.asarray(mg.load_q_mvar[bus], dtype=float).copy() for bus in mg.buses])
        bus_index = {bus: idx for idx, bus in enumerate(mg.buses)}
        for bus in mg.wind_available_mw:
            bus_p_demand[bus_index[bus], :] -= wind_active_by_bus[bus]
            bus_q_demand[bus_index[bus], :] -= wind_reactive_by_bus[bus]
        for bus in mg.pv_available_mw:
            bus_p_demand[bus_index[bus], :] -= pv_active_by_bus[bus]
            bus_q_demand[bus_index[bus], :] -= pv_reactive_by_bus[bus]
        bus_p_demand[bus_index[mg.storage.bus], :] += p_ch - p_dis
        bus_q_demand[bus_index[mg.storage.bus], :] -= q_ess
        bus_q_demand[bus_index[mg.svg_bus], :] -= q_svg
        voltage = np.zeros((len(mg.buses), T))
        for b_idx, bus in enumerate(mg.buses):
            voltage[b_idx, :] = np.sqrt(np.maximum(0.0, arr("v", [(bus, t) for t in range(T)])))
        fixed_shunt_q_by_bus = {
            bus: network.shunt_q_nominal_mvar_by_bus[bus] * voltage[b_idx, :] ** 2
            for b_idx, bus in enumerate(mg.buses)
        }
        losses = np.zeros(T)
        loading = np.zeros((len(lines), T))
        sending_p = np.zeros_like(loading)
        sending_q = np.zeros_like(loading)
        receiving_p = np.zeros_like(loading)
        for l_idx, line in enumerate(lines):
            keys = [(line.name, t) for t in range(T)]
            line_loss = arr("loss", keys)
            losses += line_loss
            p_line = arr("p_line", keys)
            q_line = arr("q_line", keys)
            sending_p[l_idx] = p_line
            sending_q[l_idx] = q_line
            receiving_p[l_idx] = p_line - line_loss
            loading[l_idx, :] = np.maximum(
                np.hypot(p_line, q_line), np.hypot(p_line - line_loss, q_line),
            ) / line.s_max_mva
        # Legacy LinDistFlow omits series reactive losses: its two branch ends
        # carry the same Q. This is a model approximation, not an AC measurement.
        receiving_q = sending_q.copy()
        reactive_losses = np.sum(sending_q - receiving_q, axis=0)
        apparent = np.sqrt(p_grid ** 2 + q_grid ** 2)
        pf = np.divide(np.abs(p_grid), apparent, out=np.ones_like(p_grid), where=apparent > 1e-8)
        wind_accounting = account_renewable(
            wind_available,
            sum(
                renewable_active_power_limits(
                    mg.wind_available_mw,
                    mg.wind_capacity_mw,
                    T,
                    label=f"{mg.name}:wind",
                ).values(),
                np.zeros(T),
            ),
            p_wind,
        )
        pv_accounting = account_renewable(
            pv_available,
            sum(
                renewable_active_power_limits(
                    mg.pv_available_mw,
                    mg.pv_capacity_mw,
                    T,
                    label=f"{mg.name}:pv",
                ).values(),
                np.zeros(T),
            ),
            p_pv,
        )
        costs = evaluate_economics(
            price_cny_per_mwh=case.price_cny_per_mwh,
            import_mw=p_grid,
            curtailed_mw=np.asarray(wind_accounting.curtailed_mw) + pv_accounting.curtailed_mw,
            charge_mw=p_ch,
            discharge_mw=p_dis,
            loss_mw=losses,
            dt_hours=a.dt_hours,
            rates=CostRates(
                a.curtailment_cost_cny_per_mwh,
                a.storage_degradation_cny_per_mwh,
                a.loss_value_cny_per_mwh,
            ),
        )
        if target is not None:
            p_reference = np.asarray(target["p_mw"], dtype=float)
            q_reference = np.asarray(target["q_mvar"], dtype=float)
            p_tracking_rmse = float(np.sqrt(np.mean((p_grid - p_reference) ** 2)))
            q_tracking_rmse = float(np.sqrt(np.mean((q_grid - q_reference) ** 2)))
        else:
            p_reference = p_grid.copy()
            q_reference = q_grid.copy()
            p_tracking_rmse = 0.0
            q_tracking_rmse = 0.0
        result_mg[mg.name] = {
            "p_grid_mw": p_grid,
            "q_grid_mvar": q_grid,
            "power_factor": pf,
            "wind_available_mw": wind_available,
            "wind_used_mw": p_wind,
            "wind_q_mvar": q_wind,
            "wind_active_by_bus_mw": wind_active_by_bus,
            "wind_reactive_by_bus_mvar": wind_reactive_by_bus,
            "pv_available_mw": pv_available,
            "pv_used_mw": p_pv,
            "pv_q_mvar": q_pv,
            "pv_active_by_bus_mw": pv_active_by_bus,
            "pv_reactive_by_bus_mvar": pv_reactive_by_bus,
            "storage_charge_mw": p_ch,
            "storage_discharge_mw": p_dis,
            "storage_active_mw": storage_active,
            "storage_energy_mwh": energy,
            "storage_q_mvar": q_ess,
            "storage_reactive_mvar": q_ess,
            "svg_q_mvar": q_svg,
            "svg_reactive_mvar": q_svg,
            "resource_schedules": resource_schedules,
            "voltage_pu": voltage,
            "line_loading_pu": loading,
            "line_sending_p_mw": sending_p,
            "line_sending_q_mvar": sending_q,
            "line_receiving_p_mw": receiving_p,
            "line_receiving_q_mvar": receiving_q,
            "line_ids": tuple(line.branch_id for line in lines),
            "line_parent_bus_ids": tuple(line.parent for line in lines),
            "line_child_bus_ids": tuple(line.child for line in lines),
            "line_tap_ratios": np.asarray([line.tap_ratio for line in lines]),
            "loss_mw": losses,
            "reactive_loss_mvar": reactive_losses,
            "p_grid_security_floor_mw": p_floor,
            "p_grid_security_headroom_mw": p_grid - p_floor,
            "p_reference_mw": p_reference,
            "q_reference_mvar": q_reference,
            "p_tracking_rmse_mw": p_tracking_rmse,
            "q_tracking_rmse_mvar": q_tracking_rmse,
            "bus_p_demand_mw": bus_p_demand,
            "bus_q_demand_mvar": bus_q_demand,
            "network_operating_mode_id": network.operating_mode_id,
            "network_contingency_id": network.contingency_id,
            "fixed_shunt_q_nominal_mvar_by_bus": dict(network.shunt_q_nominal_mvar_by_bus),
            "fixed_shunt_q_mvar_by_bus": fixed_shunt_q_by_bus,
            "economic_accounting_version": ACCOUNTING_VERSION,
            "wind_dispatchable_available_mw": wind_accounting.available_mw,
            "pv_dispatchable_available_mw": pv_accounting.available_mw,
            "wind_nameplate_excess_mw": wind_accounting.nameplate_excess_mw,
            "pv_nameplate_excess_mw": pv_accounting.nameplate_excess_mw,
            "wind_dispatch_curtailment_mw": wind_accounting.curtailed_mw,
            "pv_dispatch_curtailment_mw": pv_accounting.curtailed_mw,
            "import_cost_cny": costs.import_cost_cny,
            "curtailment_cost_cny": costs.curtailment_cost_cny,
            "storage_degradation_cost_cny": costs.storage_degradation_cost_cny,
            "loss_cost_cny": costs.loss_cost_cny,
            "economic_cost_cny": costs.economic_cost_cny,
        }

    cluster_p = np.sum([result_mg[mg.name]["p_grid_mw"] for mg in selected], axis=0)
    cluster: Dict[str, object] = {
        "total_import_mw": cluster_p,
        "peak_import_mw": float(np.max(cluster_p)),
        "max_ramp_mw_per_step": float(np.max(np.abs(np.diff(cluster_p)))) if T > 1 else 0.0,
        "economic_cost_cny": float(
            sum(float(result_mg[mg.name]["economic_cost_cny"]) for mg in selected)
        ),
        "total_active_loss_mwh": float(
            sum(np.sum(np.asarray(result_mg[mg.name]["loss_mw"])) * a.dt_hours for mg in selected)
        ),
    }
    mip_gap_value = getattr(raw, "mip_gap", None)
    return OptimizationResult(
        success=bool(raw.success),
        status=int(raw.status),
        message=str(raw.message),
        objective_cny=float(raw.fun + curtailment_constant),
        solver_objective_without_constants_cny=float(raw.fun),
        mip_gap=float(mip_gap_value) if mip_gap_value is not None else None,
        microgrids=result_mg,
        cluster=cluster,
        model_size={
            "variables": len(model.names),
            "constraints": len(model.rows),
            "binary_variables": int(sum(model.integrality)),
        },
    )
