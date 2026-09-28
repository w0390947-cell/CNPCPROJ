"""基于精确Branch Flow结构的混合整数二阶锥调度模型。

模型使用PySCIPOpt/SCIP求解。与旧LinDistFlow MILP不同，本模型通过同一个
线路电流平方变量同时描述：

* 有功损耗 ``r * ell``；
* 无功损耗 ``x * ell``；
* 完整电压降中的 ``(r**2 + x**2) * ell`` 项；
* 旋转二阶锥 ``P**2 + Q**2 <= v_parent * ell``。

对于径向网络和单调递增的网损目标，该锥松弛通常在最优点取紧；代码仍会
显式报告锥松弛间隙，并使用独立AC前推回代复核，避免把“通常精确”当成假设。
"""

from __future__ import annotations

from math import acos, cos, pi, sin, tan
from typing import Dict, Hashable, Iterable, Mapping, Sequence, cast

import numpy as np
from pyscipopt import Model, Variable, quicksum

from .data import MicrogridData, ProjectCase, renewable_active_power_limits
from .model import OptimizationResult
from .modules.dispatch.api import account_renewable, evaluate_economics
from .modules.dispatch.contracts import ACCOUNTING_VERSION, CostRates, PCCTrackingLimits
from .network_model import validate_legacy_network_alignment
from .planning_security import resolve_security_floors
from .resource_control_contracts import (
    ResourceSchedule,
    ResourceType,
    validate_resource_schedules,
)


def _selected_microgrids(
    case: ProjectCase, names: Sequence[str] | None
) -> list[MicrogridData]:
    if names is None:
        return list(case.microgrids)
    lookup = {mg.name: mg for mg in case.microgrids}
    missing = [name for name in names if name not in lookup]
    if missing:
        raise KeyError(f"unknown microgrids: {missing}")
    return [lookup[name] for name in names]


def _value(model: Model, solution, variable) -> float:
    return float(model.getSolVal(solution, variable))


def _add_polygon_constraints(
    model: Model,
    p_expression,
    q_expression,
    s_max: float,
    sides: int,
    name: str,
) -> None:
    """容量圆的安全内接多边形；仅电流—功率关系保留二阶锥。"""
    rhs = s_max * cos(pi / sides)
    for k in range(sides):
        angle = 2.0 * pi * k / sides
        model.addCons(
            cos(angle) * p_expression + sin(angle) * q_expression <= rhs,
            name=f"{name}.poly[{k}]",
        )


def solve_case_misocp(
    case: ProjectCase,
    microgrid_names: Sequence[str] | None = None,
    *,
    storage_enabled: bool = True,
    cluster_coordination: bool = True,
    pcc_targets: Mapping[str, Mapping[str, np.ndarray]] | None = None,
    tracking_limits: PCCTrackingLimits | None = None,
    p_tracking_penalty_cny_per_mw: float = 20_000.0,
    q_tracking_penalty_cny_per_mvar: float = 8_000.0,
    time_limit_seconds: float = 300.0,
    relative_gap: float = 1e-4,
    relax_storage_binaries: bool = False,
    extra_loss_tightening_cny_per_mwh: float = 0.0,
    voltage_security_margin_pu: float = 0.0,
    display_solver_output: bool = False,
    p_grid_security_floors_mw: Mapping[str, np.ndarray] | None = None,
) -> OptimizationResult:
    """建立并求解集中式或单区域MISOCP Branch Flow模型。"""
    selected = _selected_microgrids(case, microgrid_names)
    if not selected:
        raise ValueError("at least one microgrid is required")
    networks = {
        microgrid.name: validate_legacy_network_alignment(microgrid)
        for microgrid in selected
    }
    security_floors = resolve_security_floors(case, selected, p_grid_security_floors_mw)
    T = len(case.time_hours)
    a = case.assumptions
    if extra_loss_tightening_cny_per_mwh < 0.0:
        raise ValueError("extra_loss_tightening_cny_per_mwh must be nonnegative")
    if voltage_security_margin_pu < 0.0:
        raise ValueError("voltage_security_margin_pu must be nonnegative")
    model = Model("oilfield_branch_flow_misocp")
    model.setIntParam("parallel/maxnthreads", 1)
    model.setRealParam("limits/time", float(time_limit_seconds))
    model.setRealParam("limits/gap", float(relative_gap))
    model.setRealParam("numerics/feastol", 1e-7)
    if not display_solver_output:
        model.hideOutput()

    refs: Dict[str, Dict[str, Dict[Hashable, Variable]]] = {}
    objective_terms = []
    curtailment_constant = 0.0

    for mg in selected:
        network = networks[mg.name]
        lines = network.branches
        shunt_q_nominal_by_bus = network.shunt_q_nominal_mvar_by_bus
        wind_limits = renewable_active_power_limits(
            mg.wind_available_mw,
            mg.wind_capacity_mw,
            T,
            label=f"{mg.name}:wind",
        )
        pv_limits = renewable_active_power_limits(
            mg.pv_available_mw,
            mg.pv_capacity_mw,
            T,
            label=f"{mg.name}:pv",
        )
        r: Dict[str, Dict[Hashable, Variable]] = {
            "p_grid": {},
            "q_grid": {},
            "v": {},
            "vdev": {},
            "p_grid_ramp": {},
            "p_target_dev": {},
            "q_target_dev": {},
            "p_line": {},
            "q_line": {},
            "ell": {},
            "p_wind": {},
            "q_wind": {},
            "p_pv": {},
            "q_pv": {},
            "p_ch": {},
            "p_dis": {},
            "q_ess": {},
            "charge_mode": {},
            "energy": {},
            "q_svg": {},
        }
        refs[mg.name] = r
        target = pcc_targets.get(mg.name) if pcc_targets is not None else None
        if tracking_limits is not None and target is None:
            raise ValueError(
                f"{mg.name} requires PCC targets when tracking limits are enabled"
            )
        if target is not None:
            if "p_mw" not in target or "q_mvar" not in target:
                raise KeyError(f"{mg.name} target requires p_mw and q_mvar")
            if any(
                np.shape(target[key]) != (T,) or not np.isfinite(target[key]).all()
                for key in ("p_mw", "q_mvar")
            ):
                raise ValueError(
                    f"{mg.name} targets must be finite vectors of length {T}"
                )

        p_floor = security_floors[mg.name]
        pf_tan = tan(acos(a.pf_dispatch_target))
        for t in range(T):
            p_min = float(p_floor[t])
            p_grid = model.addVar(
                name=f"{mg.name}.p_grid[{t}]",
                lb=p_min,
                ub=mg.p_grid_max_mw,
            )
            q_grid = model.addVar(
                name=f"{mg.name}.q_grid[{t}]",
                lb=-mg.p_grid_max_mw,
                ub=mg.p_grid_max_mw,
            )
            r["p_grid"][t] = p_grid
            r["q_grid"][t] = q_grid
            objective_terms.append(case.price_cny_per_mwh[t] * a.dt_hours * p_grid)
            model.addCons(
                q_grid <= pf_tan * (p_grid - p_min), name=f"{mg.name}.pf_pos[{t}]"
            )
            model.addCons(
                -q_grid <= pf_tan * (p_grid - p_min), name=f"{mg.name}.pf_neg[{t}]"
            )

            if target is not None:
                dp = model.addVar(
                    name=f"{mg.name}.p_target_dev[{t}]",
                    lb=0.0,
                    ub=tracking_limits.p_mw if tracking_limits is not None else None,
                )
                dq = model.addVar(
                    name=f"{mg.name}.q_target_dev[{t}]",
                    lb=0.0,
                    ub=tracking_limits.q_mvar if tracking_limits is not None else None,
                )
                r["p_target_dev"][t] = dp
                r["q_target_dev"][t] = dq
                p_ref = float(target["p_mw"][t])
                q_ref = float(target["q_mvar"][t])
                model.addCons(p_grid - p_ref <= dp, name=f"{mg.name}.p_target_up[{t}]")
                model.addCons(
                    p_ref - p_grid <= dp, name=f"{mg.name}.p_target_down[{t}]"
                )
                model.addCons(q_grid - q_ref <= dq, name=f"{mg.name}.q_target_up[{t}]")
                model.addCons(
                    q_ref - q_grid <= dq, name=f"{mg.name}.q_target_down[{t}]"
                )
                objective_terms.extend(
                    [
                        p_tracking_penalty_cny_per_mw * a.dt_hours * dp,
                        q_tracking_penalty_cny_per_mvar * a.dt_hours * dq,
                    ]
                )

            for bus in mg.buses:
                if bus == mg.pcc_bus:
                    lo = hi = 1.0
                else:
                    tightened_min = mg.voltage_min_pu + voltage_security_margin_pu
                    tightened_max = mg.voltage_max_pu - voltage_security_margin_pu
                    if tightened_min >= tightened_max:
                        raise ValueError(
                            "voltage security margin leaves an empty voltage range"
                        )
                    lo, hi = tightened_min**2, tightened_max**2
                voltage = model.addVar(name=f"{mg.name}.v2[{bus},{t}]", lb=lo, ub=hi)
                vdev = model.addVar(name=f"{mg.name}.vdev[{bus},{t}]", lb=0.0)
                r["v"][(bus, t)] = voltage
                r["vdev"][(bus, t)] = vdev
                model.addCons(
                    voltage - 1.0 <= vdev, name=f"{mg.name}.vdev_up[{bus},{t}]"
                )
                model.addCons(
                    1.0 - voltage <= vdev, name=f"{mg.name}.vdev_down[{bus},{t}]"
                )
                objective_terms.append(
                    a.voltage_deviation_cost_cny_per_pu2h * a.dt_hours * vdev
                )

            for line in lines:
                key = (line.name, t)
                s = line.s_max_mva
                p_line = model.addVar(
                    name=f"{mg.name}.p_line[{line.name},{t}]", lb=-s, ub=s
                )
                q_line = model.addVar(
                    name=f"{mg.name}.q_line[{line.name},{t}]", lb=-s, ub=s
                )
                ell_max = s * s * line.tap_ratio**2 / max(mg.voltage_min_pu**2, 1e-6)
                ell = model.addVar(
                    name=f"{mg.name}.ell[{line.name},{t}]", lb=0.0, ub=ell_max
                )
                r["p_line"][key] = p_line
                r["q_line"][key] = q_line
                r["ell"][key] = ell
                v_parent = r["v"][(line.parent, t)]
                # 热稳、变流器容量使用保守多边形；决定损耗精度的Branch
                # Flow电流—功率关系保留旋转二阶锥。
                _add_polygon_constraints(
                    model,
                    p_line,
                    q_line,
                    s,
                    a.polygon_sides,
                    f"{mg.name}.thermal[{line.name},{t}]",
                )
                # 内部反向潮流时接收端视在功率可能更大；两端均须满足容量。
                # P/Q已在理想变压器后的串联支路侧，无需再次乘变比。
                _add_polygon_constraints(
                    model,
                    p_line - line.r_pu * ell / mg.base_mva,
                    q_line - line.x_pu * ell / mg.base_mva,
                    s,
                    a.polygon_sides,
                    f"{mg.name}.thermal_receiving[{line.name},{t}]",
                )
                model.addCons(
                    p_line * p_line + q_line * q_line
                    <= v_parent * ell / line.tap_ratio**2,
                    name=f"{mg.name}.current_cone[{line.name},{t}]",
                )
                v_child = r["v"][(line.child, t)]
                model.addCons(
                    v_child
                    == v_parent / line.tap_ratio**2
                    - 2.0 * (line.r_pu * p_line + line.x_pu * q_line) / mg.base_mva
                    + (line.r_pu**2 + line.x_pu**2) * ell / (mg.base_mva**2),
                    name=f"{mg.name}.voltage_drop[{line.name},{t}]",
                )
                objective_terms.append(
                    (a.loss_value_cny_per_mwh + extra_loss_tightening_cny_per_mwh)
                    * a.dt_hours
                    * line.r_pu
                    * ell
                    / mg.base_mva
                )

            for bus, available in mg.wind_available_mw.items():
                key = (bus, t)
                cap = mg.wind_capacity_mva[bus]
                pw = model.addVar(
                    name=f"{mg.name}.p_wind[{bus},{t}]",
                    lb=0.0,
                    ub=float(wind_limits[bus][t]),
                )
                q_cap = mg.wind_reactive_capability(bus).absolute_limit_mvar
                qw = model.addVar(
                    name=f"{mg.name}.q_wind[{bus},{t}]", lb=-q_cap, ub=q_cap
                )
                r["p_wind"][key] = pw
                r["q_wind"][key] = qw
                _add_polygon_constraints(
                    model,
                    pw,
                    qw,
                    cap,
                    a.polygon_sides,
                    f"{mg.name}.wind_cap[{bus},{t}]",
                )
                q_over_p = mg.wind_q_over_p_limit(bus)
                if q_over_p is not None:
                    if q_over_p < 0.0:
                        raise ValueError(
                            "wind Q/P capability ratio must be nonnegative"
                        )
                    model.addCons(
                        qw <= q_over_p * pw,
                        name=f"{mg.name}.wind_qp_pos[{bus},{t}]",
                    )
                    model.addCons(
                        -qw <= q_over_p * pw,
                        name=f"{mg.name}.wind_qp_neg[{bus},{t}]",
                    )
                objective_terms.append(
                    -a.curtailment_cost_cny_per_mwh * a.dt_hours * pw
                )
                curtailment_constant += (
                    a.curtailment_cost_cny_per_mwh
                    * float(wind_limits[bus][t])
                    * a.dt_hours
                )

            for bus, available in mg.pv_available_mw.items():
                key = (bus, t)
                cap = mg.pv_capacity_mva[bus]
                ppv = model.addVar(
                    name=f"{mg.name}.p_pv[{bus},{t}]",
                    lb=0.0,
                    ub=float(pv_limits[bus][t]),
                )
                pv_q_cap = cap if mg.pv_can_control_reactive(bus) else 0.0
                qpv = model.addVar(
                    name=f"{mg.name}.q_pv[{bus},{t}]",
                    lb=-pv_q_cap,
                    ub=pv_q_cap,
                )
                r["p_pv"][key] = ppv
                r["q_pv"][key] = qpv
                _add_polygon_constraints(
                    model,
                    ppv,
                    qpv,
                    cap,
                    a.polygon_sides,
                    f"{mg.name}.pv_cap[{bus},{t}]",
                )
                objective_terms.append(
                    -a.curtailment_cost_cny_per_mwh * a.dt_hours * ppv
                )
                curtailment_constant += (
                    a.curtailment_cost_cny_per_mwh
                    * float(pv_limits[bus][t])
                    * a.dt_hours
                )

            st = mg.storage
            pmax = st.p_max_mw if storage_enabled else 0.0
            qmax = (
                st.s_max_mva if storage_enabled and mg.storage_reactive_enabled else 0.0
            )
            pch = model.addVar(name=f"{mg.name}.p_ch[{t}]", lb=0.0, ub=pmax)
            pdis = model.addVar(name=f"{mg.name}.p_dis[{t}]", lb=0.0, ub=pmax)
            qess = model.addVar(name=f"{mg.name}.q_ess[{t}]", lb=-qmax, ub=qmax)
            mode = model.addVar(
                name=f"{mg.name}.charge_mode[{t}]",
                vtype="B" if storage_enabled and not relax_storage_binaries else "C",
                lb=0.0,
                ub=1.0 if storage_enabled else 0.0,
            )
            r["p_ch"][t] = pch
            r["p_dis"][t] = pdis
            r["q_ess"][t] = qess
            r["charge_mode"][t] = mode
            reserve = case.storage_reserves.get(mg.name) if storage_enabled else None
            if reserve is not None:
                for direction, offset in (
                    ("up", reserve.up_mw[t]),
                    ("down", -reserve.down_mw[t]),
                ):
                    _add_polygon_constraints(
                        model,
                        pdis - pch + offset,
                        qess,
                        st.s_max_mva,
                        a.polygon_sides,
                        f"{mg.name}.reserve_{direction}_cap[{t}]",
                    )
                model.addCons(
                    pdis - pch >= reserve.minimum_power_mw[t],
                    name=f"{mg.name}.reserve_power_min[{t}]",
                )
                model.addCons(
                    pdis - pch <= reserve.maximum_power_mw[t],
                    name=f"{mg.name}.reserve_power_max[{t}]",
                )
            model.addCons(pch <= pmax * mode, name=f"{mg.name}.charge_gate[{t}]")
            model.addCons(
                pdis <= pmax * (1.0 - mode), name=f"{mg.name}.discharge_gate[{t}]"
            )
            if storage_enabled:
                _add_polygon_constraints(
                    model,
                    pdis - pch,
                    qess,
                    st.s_max_mva,
                    a.polygon_sides,
                    f"{mg.name}.ess_cap[{t}]",
                )
            else:
                model.addCons(qess == 0.0, name=f"{mg.name}.ess_q_off[{t}]")
            objective_terms.append(
                a.storage_degradation_cny_per_mwh * a.dt_hours * (pch + pdis)
            )

            qsvg = model.addVar(
                name=f"{mg.name}.q_svg[{t}]",
                lb=mg.svg_capability().effective_q_min_mvar,
                ub=mg.svg_capability().effective_q_max_mvar,
            )
            r["q_svg"][t] = qsvg

        for t in range(1, T):
            ramp = model.addVar(name=f"{mg.name}.p_grid_ramp[{t}]", lb=0.0)
            r["p_grid_ramp"][t] = ramp
            model.addCons(
                r["p_grid"][t] - r["p_grid"][t - 1] <= ramp,
                name=f"{mg.name}.ramp_up[{t}]",
            )
            model.addCons(
                r["p_grid"][t - 1] - r["p_grid"][t] <= ramp,
                name=f"{mg.name}.ramp_down[{t}]",
            )
            objective_terms.append(a.local_import_ramp_cost_cny_per_mw * ramp)

        st = mg.storage
        for t in range(T + 1):
            if not storage_enabled or t == 0:
                lo = hi = st.e_initial_mwh
            elif t == T:
                lo = max(
                    st.e_min_mwh,
                    st.terminal_energy_mwh - a.terminal_energy_tolerance_mwh,
                )
                hi = min(
                    st.e_max_mwh,
                    st.terminal_energy_mwh + a.terminal_energy_tolerance_mwh,
                )
            else:
                lo, hi = st.e_min_mwh, st.e_max_mwh
            reserve = case.storage_reserves.get(mg.name) if storage_enabled else None
            if reserve is not None:
                lo = max(lo, reserve.energy_floor_mwh[t])
                hi = min(hi, reserve.energy_ceiling_mwh[t])
                if lo > hi:
                    raise ValueError(
                        f"{mg.name} reserve and terminal energy conflict at {t}"
                    )
            r["energy"][t] = model.addVar(name=f"{mg.name}.energy[{t}]", lb=lo, ub=hi)
        for t in range(T):
            model.addCons(
                r["energy"][t + 1]
                == r["energy"][t]
                + st.eta_charge * r["p_ch"][t] * a.dt_hours
                - r["p_dis"][t] * a.dt_hours / st.eta_discharge,
                name=f"{mg.name}.energy_balance[{t}]",
            )

        for i, row in enumerate(case.reactive_plans.get(mg.name, ())):
            t = row.time_index
            active = {
                **{
                    mg.resource_id("wind", b): r["p_wind"][(b, t)]
                    for b in mg.wind_capacity_mw
                },
                **{
                    mg.resource_id("pv", b): r["p_pv"][(b, t)]
                    for b in mg.pv_capacity_mw
                },
                mg.resource_id("storage", st.bus): r["p_dis"][t] - r["p_ch"][t],
                mg.resource_id("svg", mg.svg_bus): 0.0,
            }

            def reactive_at(index):
                return {
                    **{
                        mg.resource_id("wind", b): r["q_wind"][(b, index)]
                        for b in mg.wind_capacity_mw
                    },
                    **{
                        mg.resource_id("pv", b): r["q_pv"][(b, index)]
                        for b in mg.pv_capacity_mw
                    },
                    mg.resource_id("storage", st.bus): r["q_ess"][index],
                    mg.resource_id("svg", mg.svg_bus): r["q_svg"][index],
                }[row.resource_id]

            expression = row.p_coefficient * active[
                row.resource_id
            ] + row.q_coefficient * reactive_at(t)
            if row.previous_q_coefficient:
                expression += row.previous_q_coefficient * reactive_at(t - 1)
            model.addCons(expression <= row.upper, name=f"{mg.name}.reactive_plan[{i}]")

        incoming = {bus: [] for bus in mg.buses}
        outgoing = {bus: [] for bus in mg.buses}
        for line in lines:
            incoming[line.child].append(line)
            outgoing[line.parent].append(line)
        for t in range(T):
            for bus in mg.buses:
                p_balance = 0.0
                q_balance = shunt_q_nominal_by_bus[bus] * r["v"][(bus, t)]
                for line in incoming[bus]:
                    key = (line.name, t)
                    p_balance += (
                        r["p_line"][key] - line.r_pu * r["ell"][key] / mg.base_mva
                    )
                    q_balance += (
                        r["q_line"][key] - line.x_pu * r["ell"][key] / mg.base_mva
                    )
                for line in outgoing[bus]:
                    key = (line.name, t)
                    p_balance -= r["p_line"][key]
                    q_balance -= r["q_line"][key]
                if bus == mg.pcc_bus:
                    p_balance += r["p_grid"][t]
                    q_balance += r["q_grid"][t]
                if (bus, t) in r["p_wind"]:
                    p_balance += r["p_wind"][(bus, t)]
                    q_balance += r["q_wind"][(bus, t)]
                if (bus, t) in r["p_pv"]:
                    p_balance += r["p_pv"][(bus, t)]
                    q_balance += r["q_pv"][(bus, t)]
                if bus == mg.storage.bus:
                    p_balance += r["p_dis"][t] - r["p_ch"][t]
                    q_balance += r["q_ess"][t]
                if bus == mg.svg_bus:
                    q_balance += r["q_svg"][t]
                model.addCons(
                    p_balance == float(mg.load_p_mw[bus][t]),
                    name=f"{mg.name}.p_balance[{bus},{t}]",
                )
                model.addCons(
                    q_balance == float(mg.load_q_mvar[bus][t]),
                    name=f"{mg.name}.q_balance[{bus},{t}]",
                )

    if cluster_coordination and len(selected) > 1:
        peak = model.addVar(name="cluster.peak_import", lb=0.0)
        objective_terms.append(a.cluster_peak_cost_cny_per_mw * peak)
        for t in range(T):
            aggregate = quicksum(refs[mg.name]["p_grid"][t] for mg in selected)
            model.addCons(
                aggregate <= case.cluster_import_limit_mw,
                name=f"cluster.import_limit[{t}]",
            )
            model.addCons(aggregate <= peak, name=f"cluster.peak[{t}]")
            if t > 0:
                ramp = model.addVar(name=f"cluster.ramp[{t}]", lb=0.0)
                previous = quicksum(refs[mg.name]["p_grid"][t - 1] for mg in selected)
                model.addCons(
                    aggregate - previous <= ramp, name=f"cluster.ramp_up[{t}]"
                )
                model.addCons(
                    previous - aggregate <= ramp, name=f"cluster.ramp_down[{t}]"
                )
                objective_terms.append(a.cluster_ramp_cost_cny_per_mw * ramp)

    # 将弃风弃光常数显式放回目标，避免SCIP在负的“奖励型目标”上计算
    # 相对MIP间隙，使求解停止判据与最终报告的总成本保持同一尺度。
    objective_constant = model.addVar(
        name="objective.curtailment_constant",
        lb=curtailment_constant,
        ub=curtailment_constant,
    )
    model.setObjective(quicksum(objective_terms) + objective_constant, "minimize")
    # Same SCIP solve, with the GIL released so the owner-death guard can run.
    model.optimizeNogil()
    status_text = str(model.getStatus())
    solution = model.getBestSol()
    has_solution = solution is not None and model.getNSols() > 0
    accepted_status = status_text in {
        "optimal",
        "timelimit",
        "gaplimit",
        "bestsollimit",
    }
    success = bool(has_solution and accepted_status)
    status_code = 0 if status_text == "optimal" else (1 if success else 2)
    try:
        gap_value = float(model.getGap()) if has_solution else None
    except Exception:
        gap_value = None
    if not has_solution:
        return OptimizationResult(
            success=False,
            status=status_code,
            message=f"SCIP status: {status_text}",
            objective_cny=float("nan"),
            solver_objective_without_constants_cny=float("nan"),
            mip_gap=gap_value,
            microgrids={},
            cluster={},
            model_size={
                "variables": int(model.getNVars()),
                "constraints": int(model.getNConss()),
                "binary_variables": int(model.getNBinVars()),
            },
        )

    result_mg: Dict[str, Dict[str, object]] = {}
    for mg in selected:
        network = networks[mg.name]
        lines = network.branches
        r = refs[mg.name]
        p_floor = security_floors[mg.name]

        def arr(block: str, keys: Iterable[Hashable]) -> np.ndarray:
            return np.asarray(
                [_value(model, solution, r[block][key]) for key in keys], dtype=float
            )

        p_grid = arr("p_grid", range(T))
        q_grid = arr("q_grid", range(T))
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
            schedule.bus_id: schedule.active_power_mw for schedule in wind_schedules
        }
        wind_reactive_by_bus = {
            schedule.bus_id: schedule.reactive_power_mvar for schedule in wind_schedules
        }
        pv_active_by_bus = {
            schedule.bus_id: schedule.active_power_mw for schedule in pv_schedules
        }
        pv_reactive_by_bus = {
            schedule.bus_id: schedule.reactive_power_mvar for schedule in pv_schedules
        }
        p_wind = (
            np.sum(np.vstack(tuple(wind_active_by_bus.values())), axis=0)
            if wind_active_by_bus
            else np.zeros(T)
        )
        q_wind = (
            np.sum(np.vstack(tuple(wind_reactive_by_bus.values())), axis=0)
            if wind_reactive_by_bus
            else np.zeros(T)
        )
        p_pv = (
            np.sum(np.vstack(tuple(pv_active_by_bus.values())), axis=0)
            if pv_active_by_bus
            else np.zeros(T)
        )
        q_pv = (
            np.sum(np.vstack(tuple(pv_reactive_by_bus.values())), axis=0)
            if pv_reactive_by_bus
            else np.zeros(T)
        )
        wind_available = (
            np.sum(np.vstack(tuple(mg.wind_available_mw.values())), axis=0)
            if mg.wind_available_mw
            else np.zeros(T)
        )
        pv_available = (
            np.sum(np.vstack(tuple(mg.pv_available_mw.values())), axis=0)
            if mg.pv_available_mw
            else np.zeros(T)
        )
        p_ch = np.maximum(0.0, arr("p_ch", range(T)))
        p_dis = np.maximum(0.0, arr("p_dis", range(T)))
        q_ess = arr("q_ess", range(T))
        relaxed_charge_mode = arr("charge_mode", range(T))
        charge_mode = np.where(
            p_ch > 1e-7,
            1.0,
            np.where(p_dis > 1e-7, 0.0, np.rint(relaxed_charge_mode)),
        )
        energy = arr("energy", range(T + 1))
        q_svg = arr("q_svg", range(T))
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
        voltage_sq = np.vstack(
            [arr("v", [(bus, t) for t in range(T)]) for bus in mg.buses]
        )
        voltage = np.sqrt(np.maximum(voltage_sq, 0.0))
        fixed_shunt_q_by_bus = {
            bus: network.shunt_q_nominal_mvar_by_bus[bus] * voltage_sq[b_idx]
            for b_idx, bus in enumerate(mg.buses)
        }

        n_lines = len(lines)
        p_line = np.zeros((n_lines, T))
        q_line = np.zeros((n_lines, T))
        ell = np.zeros((n_lines, T))
        p_loss_line = np.zeros((n_lines, T))
        q_loss_line = np.zeros((n_lines, T))
        cone_gap = np.zeros((n_lines, T))
        cone_relative_gap = np.zeros((n_lines, T))
        loading = np.zeros((n_lines, T))
        bus_index = {bus: idx for idx, bus in enumerate(mg.buses)}
        for idx, line in enumerate(lines):
            keys = [(line.name, t) for t in range(T)]
            p_line[idx] = arr("p_line", keys)
            q_line[idx] = arr("q_line", keys)
            ell[idx] = arr("ell", keys)
            p_loss_line[idx] = line.r_pu * ell[idx] / mg.base_mva
            q_loss_line[idx] = line.x_pu * ell[idx] / mg.base_mva
            parent_v = voltage_sq[bus_index[line.parent]] / line.tap_ratio**2
            lhs = p_line[idx] ** 2 + q_line[idx] ** 2
            cone_gap[idx] = np.maximum(0.0, parent_v * ell[idx] - lhs)
            cone_relative_gap[idx] = cone_gap[idx] / np.maximum(1.0, lhs)
            receiving_s = np.hypot(
                p_line[idx] - p_loss_line[idx],
                q_line[idx] - q_loss_line[idx],
            )
            loading[idx] = np.maximum(np.sqrt(lhs), receiving_s) / line.s_max_mva
        losses = np.sum(p_loss_line, axis=0)
        reactive_losses = np.sum(q_loss_line, axis=0)

        bus_p_demand = np.vstack(
            [np.asarray(mg.load_p_mw[bus], dtype=float).copy() for bus in mg.buses]
        )
        bus_q_demand = np.vstack(
            [np.asarray(mg.load_q_mvar[bus], dtype=float).copy() for bus in mg.buses]
        )
        for bus in mg.wind_available_mw:
            bus_p_demand[bus_index[bus]] -= wind_active_by_bus[bus]
            bus_q_demand[bus_index[bus]] -= wind_reactive_by_bus[bus]
        for bus in mg.pv_available_mw:
            bus_p_demand[bus_index[bus]] -= pv_active_by_bus[bus]
            bus_q_demand[bus_index[bus]] -= pv_reactive_by_bus[bus]
        bus_p_demand[bus_index[mg.storage.bus]] += p_ch - p_dis
        bus_q_demand[bus_index[mg.storage.bus]] -= q_ess
        bus_q_demand[bus_index[mg.svg_bus]] -= q_svg

        apparent = np.hypot(p_grid, q_grid)
        pf = np.divide(
            np.abs(p_grid), apparent, out=np.ones_like(p_grid), where=apparent > 1e-9
        )
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
            curtailed_mw=np.asarray(wind_accounting.curtailed_mw)
            + pv_accounting.curtailed_mw,
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
        target = pcc_targets.get(mg.name) if pcc_targets is not None else None
        if target is not None:
            p_reference = np.asarray(target["p_mw"], dtype=float)
            q_reference = np.asarray(target["q_mvar"], dtype=float)
            p_tracking_rmse = float(np.sqrt(np.mean((p_grid - p_reference) ** 2)))
            q_tracking_rmse = float(np.sqrt(np.mean((q_grid - q_reference) ** 2)))
        else:
            p_reference = p_grid.copy()
            q_reference = q_grid.copy()
            p_tracking_rmse = q_tracking_rmse = 0.0

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
            "storage_charge_mode": charge_mode,
            "storage_charge_mode_relaxed": relaxed_charge_mode,
            "maximum_simultaneous_storage_mw": float(np.max(np.minimum(p_ch, p_dis))),
            "svg_q_mvar": q_svg,
            "svg_reactive_mvar": q_svg,
            "resource_schedules": resource_schedules,
            "voltage_pu": voltage,
            "line_loading_pu": loading,
            "line_ids": tuple(line.branch_id for line in lines),
            "line_parent_bus_ids": tuple(line.parent for line in lines),
            "line_child_bus_ids": tuple(line.child for line in lines),
            "line_tap_ratios": np.asarray([line.tap_ratio for line in lines]),
            "line_p_mw": p_line,
            "line_q_mvar": q_line,
            "line_sending_p_mw": p_line.copy(),
            "line_sending_q_mvar": q_line.copy(),
            "line_receiving_p_mw": p_line - p_loss_line,
            "line_receiving_q_mvar": q_line - q_loss_line,
            "line_current_squared_mva2": ell,
            "line_p_loss_mw": p_loss_line,
            "line_q_loss_mvar": q_loss_line,
            "loss_mw": losses,
            "reactive_loss_mvar": reactive_losses,
            "cone_gap_mva2": cone_gap,
            "cone_relative_gap": cone_relative_gap,
            "maximum_cone_gap_mva2": float(np.max(cone_gap)),
            "maximum_cone_relative_gap": float(np.max(cone_relative_gap)),
            "p_reference_mw": p_reference,
            "q_reference_mvar": q_reference,
            "p_tracking_rmse_mw": p_tracking_rmse,
            "q_tracking_rmse_mvar": q_tracking_rmse,
            "p_grid_security_floor_mw": p_floor.copy(),
            "p_grid_security_headroom_mw": p_grid - p_floor,
            "bus_p_demand_mw": bus_p_demand,
            "bus_q_demand_mvar": bus_q_demand,
            "network_operating_mode_id": network.operating_mode_id,
            "network_contingency_id": network.contingency_id,
            "fixed_shunt_q_nominal_mvar_by_bus": dict(
                network.shunt_q_nominal_mvar_by_bus
            ),
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

    cluster_p = np.sum(
        [np.asarray(result_mg[mg.name]["p_grid_mw"]) for mg in selected], axis=0
    )
    cluster_q = np.sum(
        [np.asarray(result_mg[mg.name]["q_grid_mvar"]) for mg in selected], axis=0
    )
    cluster: Dict[str, object] = {
        "total_import_mw": cluster_p,
        "total_reactive_import_mvar": cluster_q,
        "peak_import_mw": float(np.max(cluster_p)),
        "max_ramp_mw_per_step": float(np.max(np.abs(np.diff(cluster_p))))
        if T > 1
        else 0.0,
        "economic_cost_cny": float(
            sum(
                float(cast(float, result_mg[mg.name]["economic_cost_cny"]))
                for mg in selected
            )
        ),
        "total_active_loss_mwh": float(
            sum(
                np.sum(np.asarray(result_mg[mg.name]["loss_mw"])) * a.dt_hours
                for mg in selected
            )
        ),
        "total_reactive_loss_mvarh": float(
            sum(
                np.sum(np.asarray(result_mg[mg.name]["reactive_loss_mvar"]))
                * a.dt_hours
                for mg in selected
            )
        ),
        "maximum_cone_relative_gap": float(
            max(
                float(cast(float, result_mg[mg.name]["maximum_cone_relative_gap"]))
                for mg in selected
            )
        ),
        "maximum_simultaneous_storage_mw": float(
            max(
                float(
                    cast(float, result_mg[mg.name]["maximum_simultaneous_storage_mw"])
                )
                for mg in selected
            )
        ),
        "storage_integrality_certified": bool(
            not relax_storage_binaries
            or max(
                float(
                    cast(float, result_mg[mg.name]["maximum_simultaneous_storage_mw"])
                )
                for mg in selected
            )
            <= 1e-7
        ),
        "storage_binary_relaxation_used": bool(relax_storage_binaries),
    }
    raw_objective = float(model.getObjVal())
    return OptimizationResult(
        success=success,
        status=status_code,
        message=f"SCIP status: {status_text}",
        objective_cny=raw_objective,
        solver_objective_without_constants_cny=raw_objective - curtailment_constant,
        mip_gap=gap_value,
        microgrids=result_mg,
        cluster=cluster,
        model_size={
            "variables": int(model.getNVars()),
            "constraints": int(model.getNConss()),
            "binary_variables": int(model.getNBinVars()),
        },
    )
