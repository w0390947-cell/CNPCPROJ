"""ADMM协调层使用的凸区域控制器和集群投影器。

该层有意不包含储能充放电二进制变量，以保持标准ADMM子问题的凸性。
ADMM收敛后的P/Q参考再交给独立MISOCP区域控制器落实。
"""

from __future__ import annotations

from math import acos, tan
from typing import Mapping, Sequence

import cvxpy as cp
import numpy as np

from .data import MicrogridData, ProjectCase, renewable_active_power_limits
from .hierarchy_types import ADMMConfig, CoordinationSignal, RegionalSchedule
from .modules.dispatch.api import account_renewable, evaluate_economics
from .modules.dispatch.contracts import CostRates, DispatchCapabilities


def _solve_problem(problem: cp.Problem, preferred_solver: str) -> None:
    installed = set(cp.installed_solvers())
    candidates = [preferred_solver, "CLARABEL", "OSQP", "SCS"]
    errors: list[str] = []
    for solver in dict.fromkeys(candidates):
        if solver not in installed:
            continue
        try:
            if solver == "OSQP":
                problem.solve(
                    solver=solver, warm_start=True, eps_abs=1e-6, eps_rel=1e-6, max_iter=30_000
                )
            elif solver == "SCS":
                problem.solve(solver=solver, warm_start=True, eps=1e-5, max_iters=20_000)
            else:
                problem.solve(solver=solver, warm_start=True)
            if problem.status in {cp.OPTIMAL, cp.OPTIMAL_INACCURATE}:
                return
            errors.append(f"{solver}: {problem.status}")
        except Exception as exc:  # pragma: no cover - only used when a solver backend fails
            errors.append(f"{solver}: {exc}")
    raise RuntimeError("convex solver failed; " + "; ".join(errors))


class RegionalConvexController:
    """一个只掌握本地数据的区域凸优化控制器。"""

    def __init__(
        self,
        case: ProjectCase,
        microgrid: MicrogridData,
        config: ADMMConfig,
        loss_calibration: Mapping[str, np.ndarray] | None = None,
        p_grid_security_floor_mw: np.ndarray | None = None,
        capabilities: DispatchCapabilities | None = None,
    ):
        self.case = case
        self.microgrid = microgrid
        self.config = config
        self.capabilities = capabilities or DispatchCapabilities()
        self.T = len(case.time_hours)
        self.loss_calibration = loss_calibration
        fixed_floor = max(
            microgrid.p_grid_min_mw,
            case.assumptions.no_reverse_margin_mw,
        )
        self.p_grid_security_floor_mw = np.asarray(
            p_grid_security_floor_mw
            if p_grid_security_floor_mw is not None
            else np.full(self.T, fixed_floor),
            dtype=float,
        )
        if self.p_grid_security_floor_mw.shape != (self.T,):
            raise ValueError(f"{microgrid.name} security floor must contain {self.T} time steps")
        self._build_problem()

    def _build_problem(self) -> None:
        mg = self.microgrid
        case = self.case
        a = case.assumptions
        T = self.T
        self.z_p = cp.Parameter(T)
        self.z_q = cp.Parameter(T)
        self.u_p = cp.Parameter(T)
        self.u_q = cp.Parameter(T)

        self.p_grid = cp.Variable(T, name=f"{mg.name}_p_grid")
        self.q_grid = cp.Variable(T, name=f"{mg.name}_q_grid")
        self.p_renew = cp.Variable(T, name=f"{mg.name}_p_renew")
        self.p_charge = cp.Variable(T, nonneg=True, name=f"{mg.name}_p_charge")
        self.p_discharge = cp.Variable(T, nonneg=True, name=f"{mg.name}_p_discharge")
        self.energy = cp.Variable(T + 1, name=f"{mg.name}_energy")
        self.q_support = cp.Variable(T, name=f"{mg.name}_q_support")

        load_p = np.sum(np.vstack(list(mg.load_p_mw.values())), axis=0)
        load_q = np.sum(np.vstack(list(mg.load_q_mvar.values())), axis=0)
        wind = sum(mg.wind_available_mw.values(), np.zeros(T))
        pv = sum(mg.pv_available_mw.values(), np.zeros(T))
        renewable_available = wind + pv
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
        renewable_limit = sum(wind_limits.values(), np.zeros(T)) + sum(
            pv_limits.values(),
            np.zeros(T),
        )
        self.raw_renewable_available_mw = renewable_available.copy()
        self.renewable_available_mw = renewable_limit.copy()
        # 正式分层流程使用AC一致MISOCP结果逐时校准；单独调用ADMM时回退到
        # 保守固定裕度。协调层仍保持凸性，精确网络方程由区域MISOCP落实。
        if self.loss_calibration is None:
            p_loss_allowance = 0.008 * load_p
            q_loss_allowance = 0.005 * load_q
        else:
            p_loss_allowance = np.asarray(self.loss_calibration["p_loss_mw"], dtype=float)
            q_loss_allowance = np.asarray(self.loss_calibration["q_loss_mvar"], dtype=float)
            if p_loss_allowance.shape != (T,) or q_loss_allowance.shape != (T,):
                raise ValueError(f"{mg.name} loss calibration must contain {T} time steps")
            if np.min(p_loss_allowance) < -1e-9 or np.min(q_loss_allowance) < -1e-9:
                raise ValueError(f"{mg.name} loss calibration must be nonnegative")
        st = mg.storage
        self.p_loss_allowance_mw = p_loss_allowance.copy()
        pf_tan = tan(acos(a.pf_dispatch_target))
        q_support_limit = min(
            5.0,
            max(
                abs(mg.svg_capability().effective_q_min_mvar),
                abs(mg.svg_capability().effective_q_max_mvar),
            )
            + 0.18
            * (
                sum(mg.wind_capacity_mva.values())
                + sum(mg.pv_capacity_mva.values())
                + (
                    st.s_max_mva
                    if self.capabilities.storage_enabled and mg.storage_reactive_enabled
                    else 0.0
                )
            ),
        )

        constraints = [
            self.p_grid + self.p_renew + self.p_discharge - self.p_charge
            == load_p + p_loss_allowance,
            self.q_grid + self.q_support == load_q + q_loss_allowance,
            self.p_grid >= self.p_grid_security_floor_mw,
            self.p_grid <= mg.p_grid_max_mw,
            self.q_grid <= pf_tan * (self.p_grid - self.p_grid_security_floor_mw),
            -self.q_grid <= pf_tan * (self.p_grid - self.p_grid_security_floor_mw),
            self.p_renew >= 0.0,
            self.p_renew <= renewable_limit,
            self.p_charge <= (st.p_max_mw if self.capabilities.storage_enabled else 0.0),
            self.p_discharge <= (st.p_max_mw if self.capabilities.storage_enabled else 0.0),
            self.energy[0] == st.e_initial_mwh,
            self.energy >= st.e_min_mwh,
            self.energy <= st.e_max_mwh,
            self.energy[T] >= st.e_initial_mwh - a.terminal_energy_tolerance_mwh,
            self.energy[T] <= st.e_initial_mwh + a.terminal_energy_tolerance_mwh,
            self.q_support >= -q_support_limit,
            self.q_support <= q_support_limit,
        ]
        if not self.capabilities.storage_enabled:
            constraints.append(self.energy == st.e_initial_mwh)
        for t in range(T):
            constraints.append(
                self.energy[t + 1]
                == self.energy[t]
                + st.eta_charge * self.p_charge[t] * a.dt_hours
                - self.p_discharge[t] * a.dt_hours / st.eta_discharge
            )

        price = case.price_cny_per_mwh
        local_cost = (
            cp.sum(cp.multiply(price * a.dt_hours, self.p_grid))
            + a.curtailment_cost_cny_per_mwh * a.dt_hours * cp.sum(renewable_limit - self.p_renew)
            + a.storage_degradation_cny_per_mwh
            * a.dt_hours
            * cp.sum(self.p_charge + self.p_discharge)
            + self.config.local_ramp_regularization * cp.sum_squares(cp.diff(self.p_grid))
            + self.config.local_q_regularization * cp.sum_squares(self.q_support)
        )
        augmented = (
            0.5
            * self.config.rho
            * (
                cp.sum_squares(self.p_grid - self.z_p + self.u_p)
                + cp.sum_squares(self.q_grid - self.z_q + self.u_q)
            )
        )
        self.local_cost_expression = local_cost
        self.problem = cp.Problem(cp.Minimize(local_cost + augmented), constraints)
        self.autonomous_problem = cp.Problem(cp.Minimize(local_cost), constraints)

    def solve(
        self, signal: CoordinationSignal | None, *, fallback: bool = False
    ) -> RegionalSchedule:
        if signal is None:
            problem = self.autonomous_problem
        else:
            self.z_p.value = signal.p_reference_mw
            self.z_q.value = signal.q_reference_mvar
            self.u_p.value = signal.dual_p
            self.u_q.value = signal.dual_q
            problem = self.problem
        _solve_problem(problem, self.config.solver)
        values = [
            self.p_grid.value,
            self.q_grid.value,
            self.p_renew.value,
            self.p_charge.value,
            self.p_discharge.value,
            self.energy.value,
            self.q_support.value,
        ]
        if any(value is None for value in values):
            raise RuntimeError(f"{self.microgrid.name} returned no convex solution")
        renewable_accounting = account_renewable(
            self.raw_renewable_available_mw,
            self.renewable_available_mw,
            np.asarray(self.p_renew.value, dtype=float),
        )
        a = self.case.assumptions
        economic_cost = evaluate_economics(
            price_cny_per_mwh=self.case.price_cny_per_mwh,
            import_mw=np.asarray(self.p_grid.value, dtype=float),
            curtailed_mw=renewable_accounting.curtailed_mw,
            charge_mw=np.asarray(self.p_charge.value, dtype=float),
            discharge_mw=np.asarray(self.p_discharge.value, dtype=float),
            loss_mw=self.p_loss_allowance_mw,
            dt_hours=a.dt_hours,
            rates=CostRates(
                a.curtailment_cost_cny_per_mwh,
                a.storage_degradation_cny_per_mwh,
                a.loss_value_cny_per_mwh,
            ),
        )
        return RegionalSchedule(
            name=self.microgrid.name,
            p_grid_mw=np.asarray(self.p_grid.value, dtype=float),
            q_grid_mvar=np.asarray(self.q_grid.value, dtype=float),
            renewable_used_mw=np.asarray(self.p_renew.value, dtype=float),
            storage_charge_mw=np.asarray(self.p_charge.value, dtype=float),
            storage_discharge_mw=np.asarray(self.p_discharge.value, dtype=float),
            storage_energy_mwh=np.asarray(self.energy.value, dtype=float),
            q_support_mvar=np.asarray(self.q_support.value, dtype=float),
            local_objective_cny=float(self.local_cost_expression.value),
            status=str(problem.status),
            economic_cost=economic_cost,
            renewable_accounting=renewable_accounting,
            signal_iteration=signal.iteration if signal is not None else -1,
            used_fallback=fallback,
        )


class ClusterProjectionController:
    """集群层：只接收PCC计划并执行全局可行域投影。"""

    def __init__(
        self,
        case: ProjectCase,
        microgrids: Sequence[MicrogridData],
        config: ADMMConfig,
        p_grid_security_floors_mw: Mapping[str, np.ndarray] | None = None,
    ) -> None:
        self.case = case
        self.microgrids = list(microgrids)
        self.config = config
        self.M = len(self.microgrids)
        self.T = len(case.time_hours)
        self.p_grid_security_floors_mw = p_grid_security_floors_mw
        self._build_problem()

    def _build_problem(self) -> None:
        M, T = self.M, self.T
        a = self.case.assumptions
        self.v_p = cp.Parameter((M, T))
        self.v_q = cp.Parameter((M, T))
        self.z_p = cp.Variable((M, T), name="cluster_z_p")
        self.z_q = cp.Variable((M, T), name="cluster_z_q")
        self.peak = cp.Variable(nonneg=True, name="cluster_peak")
        aggregate_p = cp.sum(self.z_p, axis=0)
        aggregate_q = cp.sum(self.z_q, axis=0)
        pf_tan = tan(acos(self.config.aggregate_pf_target))
        if self.p_grid_security_floors_mw is None:
            lower = np.vstack(
                [
                    np.full(
                        T,
                        max(mg.p_grid_min_mw, a.no_reverse_margin_mw),
                        dtype=float,
                    )
                    for mg in self.microgrids
                ]
            )
        else:
            lower = np.vstack(
                [
                    np.asarray(self.p_grid_security_floors_mw[mg.name], dtype=float)
                    for mg in self.microgrids
                ]
            )
        upper = np.asarray([mg.p_grid_max_mw for mg in self.microgrids])[:, None]
        constraints = [
            self.z_p >= lower,
            self.z_p <= upper,
            aggregate_p <= self.case.cluster_import_limit_mw,
            aggregate_q <= pf_tan * aggregate_p,
            -aggregate_q <= pf_tan * aggregate_p,
            self.peak >= aggregate_p,
        ]
        objective = (
            0.5
            * self.config.rho
            * (cp.sum_squares(self.z_p - self.v_p) + cp.sum_squares(self.z_q - self.v_q))
            + a.cluster_peak_cost_cny_per_mw * self.peak
            + a.cluster_ramp_cost_cny_per_mw * cp.norm1(cp.diff(aggregate_p))
        )
        self.problem = cp.Problem(cp.Minimize(objective), constraints)

    def project(self, v_p: np.ndarray, v_q: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        self.v_p.value = v_p
        self.v_q.value = v_q
        _solve_problem(self.problem, self.config.solver)
        if self.z_p.value is None or self.z_q.value is None:
            raise RuntimeError("cluster projection returned no solution")
        return np.asarray(self.z_p.value, dtype=float), np.asarray(self.z_q.value, dtype=float)
