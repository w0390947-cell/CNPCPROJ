"""ADMM协调层使用的凸区域控制器和集群投影器。

该层有意不包含储能充放电二进制变量，以保持标准ADMM子问题的凸性。
ADMM收敛后的P/Q参考再交给独立MISOCP区域控制器落实。
"""

from __future__ import annotations

from functools import lru_cache
from math import acos, cos, pi, sin, tan
from typing import Mapping, Sequence

import cvxpy as cp
import numpy as np
from numpy.typing import NDArray
from scipy.sparse import coo_matrix

from .data import MicrogridData, ProjectCase, renewable_active_power_limits
from .hierarchy_types import ADMMConfig, CoordinationSignal, RegionalSchedule
from .modules.dispatch.api import (
    account_renewable,
    coordination_shunt_injection,
    evaluate_economics,
    storage_reachability_rows,
)
from .modules.dispatch.contracts import (
    CoordinationNetworkCalibration,
    CostRates,
    DispatchCapabilities,
    RegionalNetworkCandidate,
)
from .network_model import validate_legacy_network_alignment


def _capacity_polygon(
    p_mw: cp.Expression, q_mvar: cp.Expression, s_mva: float, sides: int
) -> list[cp.Constraint]:
    """Same inscribed MVA envelope as regional MISOCP; see ADR 0023.

    These are SDK constraints, not a second definition of device capability.
    Resource-specific policy/absolute limits are read from MicrogridData.
    """
    return [
        cos(2 * pi * k / sides) * p_mw + sin(2 * pi * k / sides) * q_mvar
        <= s_mva * cos(pi / sides)
        for k in range(sides)
    ]


@lru_cache(maxsize=1)
def _installed_solvers() -> frozenset[str]:
    """Discover once per interpreter; environment changes require a new worker."""
    return frozenset(cp.installed_solvers())


def _solve_problem(problem: cp.Problem, preferred_solver: str) -> None:
    installed = _installed_solvers()
    candidates = [preferred_solver, "CLARABEL", "OSQP", "SCS"]
    errors: list[str] = []
    for solver in dict.fromkeys(candidates):
        if solver not in installed:
            continue
        try:
            if solver == "OSQP":
                problem.solve(
                    solver=solver,
                    warm_start=True,
                    eps_abs=1e-6,
                    eps_rel=1e-6,
                    max_iter=30_000,
                )
            elif solver == "SCS":
                problem.solve(
                    solver=solver, warm_start=True, eps=1e-5, max_iters=20_000
                )
            else:
                problem.solve(solver=solver, warm_start=True)
            if problem.status in {cp.OPTIMAL, cp.OPTIMAL_INACCURATE}:
                return
            errors.append(f"{solver}: {problem.status}")
        except (
            Exception
        ) as exc:  # pragma: no cover - only used when a solver backend fails
            errors.append(f"{solver}: {exc}")
    raise RuntimeError("convex solver failed; " + "; ".join(errors))


class RegionalConvexController:
    """一个只掌握本地数据的区域凸优化控制器。"""

    def __init__(
        self,
        case: ProjectCase,
        microgrid: MicrogridData,
        config: ADMMConfig,
        loss_calibration: CoordinationNetworkCalibration
        | Mapping[str, np.ndarray]
        | None = None,
        p_grid_security_floor_mw: np.ndarray | None = None,
        capabilities: DispatchCapabilities | None = None,
        network_candidate: RegionalNetworkCandidate | None = None,
    ):
        if case.joint_pq_plans:
            raise ValueError("joint single P/Q planning requires the MISOCP backend")
        self.case = case
        self.microgrid = microgrid
        self.config = config
        self.capabilities = capabilities or DispatchCapabilities()
        self.network_candidate = network_candidate
        self.T = len(case.time_hours)
        self.reactive_rhs: cp.Parameter | None = None
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
            raise ValueError(
                f"{microgrid.name} security floor must contain {self.T} time steps"
            )
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
        self.network_enabled = self.config.network_model == "branch-flow-socp-v1"
        self.network_load_p = {b: cp.Parameter(T) for b in mg.buses}
        self.network_load_q = {b: cp.Parameter(T) for b in mg.buses}
        self.network_loss: cp.Expression = cp.Constant(np.zeros(T))
        self.network_cost: cp.Expression = cp.Constant(0.0)

        self.p_demand = cp.Parameter(T)
        self.q_demand = cp.Parameter(T)
        self.security_floor = cp.Parameter(T)
        self.renewable_limit = cp.Parameter(T, nonneg=True)
        self.price = cp.Parameter(T)
        self.initial_energy = cp.Parameter()
        self.terminal_energy = cp.Parameter()
        self.terminal_lower = cp.Parameter()
        self.terminal_upper = cp.Parameter()
        self.reserve_energy_floor = cp.Parameter(T + 1)
        self.reserve_energy_ceiling = cp.Parameter(T + 1)
        self.reserve_power_minimum = cp.Parameter(T)
        self.reserve_power_maximum = cp.Parameter(T)
        self.wind_limits = {
            bus: cp.Parameter(T, nonneg=True) for bus in mg.wind_available_mw
        }
        self.pv_limits = {
            bus: cp.Parameter(T, nonneg=True) for bus in mg.pv_available_mw
        }
        self.update_window(
            case, mg, self.loss_calibration, self.p_grid_security_floor_mw
        )
        st = mg.storage
        pf_tan = tan(acos(a.pf_dispatch_target))
        constraints: list[cp.Constraint] = [
            self.p_grid >= self.security_floor,
            self.p_grid <= mg.p_grid_max_mw,
            self.q_grid <= pf_tan * (self.p_grid - self.security_floor),
            -self.q_grid <= pf_tan * (self.p_grid - self.security_floor),
            self.p_renew >= 0.0,
            self.p_renew <= self.renewable_limit,
            self.p_charge
            <= (st.p_max_mw if self.capabilities.storage_enabled else 0.0),
            self.p_discharge
            <= (st.p_max_mw if self.capabilities.storage_enabled else 0.0),
            self.energy[0] == self.initial_energy,
            self.energy >= st.e_min_mwh,
            self.energy <= st.e_max_mwh,
            self.energy >= self.reserve_energy_floor,
            self.energy <= self.reserve_energy_ceiling,
            self.p_discharge - self.p_charge >= self.reserve_power_minimum,
            self.p_discharge - self.p_charge <= self.reserve_power_maximum,
            self.energy[T] >= self.terminal_lower,
            self.energy[T] <= self.terminal_upper,
        ]
        if not self.network_enabled:
            constraints.extend(
                [
                    self.p_grid + self.p_renew + self.p_discharge - self.p_charge
                    == self.p_demand,
                    self.q_grid + self.q_support == self.q_demand,
                ]
            )
        if self.network_candidate is not None:
            constraints.extend(
                [
                    self.p_grid == np.asarray(self.network_candidate.p_mw),
                    self.q_grid == np.asarray(self.network_candidate.q_mvar),
                ]
            )
        reachability = (
            case.storage_reachability.get(mg.name)
            if self.capabilities.storage_enabled
            else None
        )
        if reachability is not None:
            power = self.p_discharge - self.p_charge
            for row in storage_reachability_rows(reachability):
                previous = (
                    power[row.time_index - 1] if row.previous_coefficient else 0.0
                )
                constraints.append(
                    row.coefficient * power[row.time_index]
                    + row.previous_coefficient * previous
                    <= row.upper
                )
        self._build_resources(constraints)

    def update_window(
        self,
        case: ProjectCase,
        microgrid: MicrogridData,
        loss_calibration: CoordinationNetworkCalibration
        | Mapping[str, np.ndarray]
        | None,
        security_floor: np.ndarray,
    ) -> None:
        """Refresh owned data; caller must verify structural compatibility first."""
        mg, T, a = microgrid, self.T, case.assumptions
        if len(case.time_hours) != T or security_floor.shape != (T,):
            raise ValueError("coordination window dimensions changed")
        if case.storage_reachability.get(mg.name) != self.case.storage_reachability.get(
            mg.name
        ):
            raise ValueError(
                "changed storage reachability requires a rebuilt controller"
            )
        self.case, self.microgrid = case, microgrid
        self.loss_calibration = loss_calibration
        for bus in mg.buses:
            self.network_load_p[bus].value = np.array(mg.load_p_mw[bus], copy=True)
            self.network_load_q[bus].value = np.array(mg.load_q_mvar[bus], copy=True)
        self.p_grid_security_floor_mw = np.array(security_floor, dtype=float, copy=True)

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
        # Network calibration holds reference-voltage injections fixed to retain
        # convexity. It is not an exact network model; see ADR 0037.
        network = validate_legacy_network_alignment(mg)
        calibration = (
            self.loss_calibration
            if isinstance(self.loss_calibration, CoordinationNetworkCalibration)
            else None
        )
        shunt_q: NDArray[np.float64] = (
            np.zeros(T)
            if self.network_enabled
            else np.asarray(
                coordination_shunt_injection(
                    calibration,
                    current_nominal_mvar_by_bus=network.shunt_q_nominal_mvar_by_bus,
                    steps=T,
                ),
                dtype=np.float64,
            )
        )
        p_loss_allowance: NDArray[np.float64]
        q_loss_allowance: NDArray[np.float64]
        if self.loss_calibration is None:
            p_loss_allowance = 0.008 * load_p
            q_loss_allowance = 0.005 * load_q
        elif isinstance(self.loss_calibration, CoordinationNetworkCalibration):
            p_loss_allowance = np.asarray(
                self.loss_calibration.p_loss_mw, dtype=np.float64
            )
            q_loss_allowance = np.asarray(
                self.loss_calibration.q_loss_mvar, dtype=np.float64
            )
        else:
            p_loss_allowance = np.asarray(
                self.loss_calibration["p_loss_mw"], dtype=np.float64
            )
            q_loss_allowance = np.asarray(
                self.loss_calibration["q_loss_mvar"], dtype=np.float64
            )
            if p_loss_allowance.shape != (T,) or q_loss_allowance.shape != (T,):
                raise ValueError(
                    f"{mg.name} loss calibration must contain {T} time steps"
                )
            if (
                not np.isfinite(p_loss_allowance).all()
                or not np.isfinite(q_loss_allowance).all()
            ):
                raise ValueError(f"{mg.name} loss calibration must be finite")
            if np.min(p_loss_allowance) < -1e-9 or np.min(q_loss_allowance) < -1e-9:
                raise ValueError(f"{mg.name} loss calibration must be nonnegative")
        self.p_loss_allowance_mw = p_loss_allowance.copy()
        self.fixed_shunt_q_mvar: NDArray[np.float64] = np.array(
            shunt_q, dtype=np.float64, copy=True
        )
        self.p_demand.value = load_p + p_loss_allowance
        self.q_demand.value = load_q + q_loss_allowance - shunt_q
        self.security_floor.value = self.p_grid_security_floor_mw.copy()
        self.renewable_limit.value = renewable_limit.copy()
        self.price.value = np.array(case.price_cny_per_mwh, dtype=float, copy=True)
        self.initial_energy.value = mg.storage.e_initial_mwh
        self.terminal_energy.value = mg.storage.terminal_energy_mwh
        terminal = case.storage_window_targets.get(mg.name)
        self.terminal_lower.value = (
            (
                terminal.minimum_mwh
                if terminal is not None
                else mg.storage.terminal_energy_mwh - a.terminal_energy_tolerance_mwh
            )
            if self.capabilities.storage_enabled
            else mg.storage.e_initial_mwh
        )
        self.terminal_upper.value = (
            (
                terminal.maximum_mwh
                if terminal is not None
                else mg.storage.terminal_energy_mwh + a.terminal_energy_tolerance_mwh
            )
            if self.capabilities.storage_enabled
            else mg.storage.e_initial_mwh
        )
        if self.reactive_rhs is not None:
            self.reactive_rhs.value = np.array(
                [row.upper for row in case.reactive_plans[mg.name]]
            )
        reserve = (
            case.storage_reserves.get(mg.name)
            if self.capabilities.storage_enabled
            else None
        )
        self.reserve_energy_floor.value = (
            np.array(reserve.energy_floor_mwh)
            if reserve
            else np.full(T + 1, mg.storage.e_min_mwh)
        )
        self.reserve_energy_ceiling.value = (
            np.array(reserve.energy_ceiling_mwh)
            if reserve
            else np.full(T + 1, mg.storage.e_max_mwh)
        )
        self.reserve_power_minimum.value = (
            np.array(reserve.minimum_power_mw)
            if reserve
            else np.full(T, -mg.storage.p_max_mw)
        )
        self.reserve_power_maximum.value = (
            np.array(reserve.maximum_power_mw)
            if reserve
            else np.full(T, mg.storage.p_max_mw)
        )
        for bus, parameter in self.wind_limits.items():
            parameter.value = wind_limits[bus].copy()
        for bus, parameter in self.pv_limits.items():
            parameter.value = pv_limits[bus].copy()

    def _build_resources(self, constraints: list[cp.Constraint]) -> None:
        mg, a, T = self.microgrid, self.case.assumptions, self.T
        st = mg.storage
        # Preserve individual P/Q decisions: curtailing wind also reduces kP.
        # Forecast availability is only a P upper bound, never a fixed Q budget.
        self.p_wind: dict[str, cp.Variable] = {}
        self.q_wind: dict[str, cp.Variable] = {}
        self.p_pv: dict[str, cp.Variable] = {}
        self.q_pv: dict[str, cp.Variable] = {}
        for bus, available in self.wind_limits.items():
            p = cp.Variable(T, nonneg=True, name=f"{mg.name}_wind_p_{bus}")
            q = cp.Variable(T, name=f"{mg.name}_wind_q_{bus}")
            self.p_wind[bus], self.q_wind[bus] = p, q
            capability = mg.wind_reactive_capability(bus)
            constraints.extend(
                [
                    p <= available,
                    q >= -capability.absolute_limit_mvar,
                    q <= capability.absolute_limit_mvar,
                ]
            )
            if capability.q_abs_over_p_max is not None:
                constraints.extend(
                    [
                        q <= capability.q_abs_over_p_max * p,
                        -q <= capability.q_abs_over_p_max * p,
                    ]
                )
            constraints.extend(
                _capacity_polygon(p, q, capability.s_max_mva, a.polygon_sides)
            )
        for bus, available in self.pv_limits.items():
            p = cp.Variable(T, nonneg=True, name=f"{mg.name}_pv_p_{bus}")
            q = cp.Variable(T, name=f"{mg.name}_pv_q_{bus}")
            self.p_pv[bus], self.q_pv[bus] = p, q
            constraints.append(p <= available)
            if not mg.pv_can_control_reactive(bus):
                constraints.append(q == 0)
            constraints.extend(
                _capacity_polygon(p, q, mg.pv_capacity_mva[bus], a.polygon_sides)
            )

        self.q_storage = cp.Variable(T, name=f"{mg.name}_storage_q")
        if not self.capabilities.storage_enabled or not mg.storage_reactive_enabled:
            constraints.append(self.q_storage == 0)
        if self.capabilities.storage_enabled:
            # Convex hull of the execution layer's charge/discharge gates.
            constraints.append(self.p_charge + self.p_discharge <= st.p_max_mw)
            constraints.extend(
                _capacity_polygon(
                    self.p_discharge - self.p_charge,
                    self.q_storage,
                    st.s_max_mva,
                    a.polygon_sides,
                )
            )
            # Hold Q fixed while deploying either active reserve direction.
            for offset in (
                st.p_max_mw - self.reserve_power_maximum,
                -st.p_max_mw - self.reserve_power_minimum,
            ):
                constraints.extend(
                    _capacity_polygon(
                        self.p_discharge - self.p_charge + offset,
                        self.q_storage,
                        st.s_max_mva,
                        a.polygon_sides,
                    )
                )
        self.q_svg = cp.Variable(T, name=f"{mg.name}_svg_q")
        svg = mg.svg_capability()
        constraints.extend(
            [
                self.q_svg >= svg.effective_q_min_mvar,
                self.q_svg <= svg.effective_q_max_mvar,
                self.p_renew
                == sum(
                    (*self.p_wind.values(), *self.p_pv.values()),
                    cp.Constant(np.zeros(T)),
                ),
                self.q_support
                == self.q_svg
                + self.q_storage
                + sum(
                    (*self.q_wind.values(), *self.q_pv.values()),
                    cp.Constant(np.zeros(T)),
                ),
            ]
        )
        rows = self.case.reactive_plans.get(mg.name, ())
        if rows:
            active: dict[str, cp.Expression] = {
                **{mg.resource_id("wind", b): v for b, v in self.p_wind.items()},
                **{mg.resource_id("pv", b): v for b, v in self.p_pv.items()},
                mg.resource_id("storage", st.bus): self.p_discharge - self.p_charge,
                mg.resource_id("svg", mg.svg_bus): cp.Constant(np.zeros(T)),
            }
            reactive: dict[str, cp.Expression] = {
                **{mg.resource_id("wind", b): v for b, v in self.q_wind.items()},
                **{mg.resource_id("pv", b): v for b, v in self.q_pv.items()},
                mg.resource_id("storage", st.bus): self.q_storage,
                mg.resource_id("svg", mg.svg_bus): self.q_svg,
            }
            self.reactive_rhs = cp.Parameter(
                len(rows), value=np.array([row.upper for row in rows])
            )
            ids = tuple(active)
            offsets = {rid: i * 2 * T for i, rid in enumerate(ids)}
            row_indices: list[int] = []
            column_indices: list[int] = []
            coefficients: list[float] = []
            for i, row in enumerate(rows):
                offset, t = offsets[row.resource_id], row.time_index
                for column, value in (
                    (offset + t, row.p_coefficient),
                    (offset + T + t, row.q_coefficient),
                    (offset + T + t - 1, row.previous_q_coefficient),
                ):
                    if value:
                        row_indices.append(i)
                        column_indices.append(column)
                        coefficients.append(value)
            matrix = coo_matrix(
                (coefficients, (row_indices, column_indices)),
                shape=(len(rows), 2 * len(ids) * T),
            ).tocsr()
            variables = cp.hstack(
                [value for rid in ids for value in (active[rid], reactive[rid])]
            )
            constraints.append(matrix @ variables <= self.reactive_rhs)
        if not self.capabilities.storage_enabled:
            constraints.append(self.energy == self.initial_energy)
        for t in range(T):
            constraints.append(
                self.energy[t + 1]
                == self.energy[t]
                + st.eta_charge * self.p_charge[t] * a.dt_hours
                - self.p_discharge[t] * a.dt_hours / st.eta_discharge
            )

        if self.network_enabled:
            self._build_network(constraints)
        local_cost = (
            cp.sum(cp.multiply(self.price * a.dt_hours, self.p_grid))
            + a.curtailment_cost_cny_per_mwh
            * a.dt_hours
            * cp.sum(self.renewable_limit - self.p_renew)
            + a.storage_degradation_cny_per_mwh
            * a.dt_hours
            * cp.sum(self.p_charge + self.p_discharge)
            + (
                self.config.local_ramp_regularization
                * cp.sum_squares(cp.diff(self.p_grid))
                if T > 1
                else 0.0
            )
            + self.config.local_q_regularization * cp.sum_squares(self.q_support)
            + self.network_cost
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
        if not self.problem.is_dpp() or not self.autonomous_problem.is_dpp():
            raise ValueError("regional parameterized model must be DPP")

    def _build_network(self, constraints: list[cp.Constraint]) -> None:
        """Branch-flow SOCP with the same two-ended thermal polygons as MISOCP.

        Storage modes and current equalities remain relaxed. These constraints
        inform coordination but never replace the independent AC adoption gate.
        See Network_Physical_Recovery.md and the cross-backend conservation tests.
        """
        mg, a, T = self.microgrid, self.case.assumptions, self.T
        network = validate_legacy_network_alignment(mg)
        voltage = {b: cp.Variable(T, nonneg=True) for b in mg.buses}
        p_in: dict[str, cp.Expression] = {b: cp.Constant(np.zeros(T)) for b in mg.buses}
        q_in: dict[str, cp.Expression] = {
            b: network.shunt_q_nominal_mvar_by_bus[b] * voltage[b] for b in mg.buses
        }
        losses: list[cp.Expression] = []
        v_cost: list[cp.Expression] = []
        for bus in mg.buses:
            if bus == mg.pcc_bus:
                constraints.append(voltage[bus] == 1.0)
                p_in[bus] += self.p_grid
                q_in[bus] += self.q_grid
            else:
                constraints.extend(
                    [
                        voltage[bus] >= mg.voltage_min_pu**2,
                        voltage[bus] <= mg.voltage_max_pu**2,
                    ]
                )
            v_cost.append(cp.sum(cp.abs(voltage[bus] - 1.0)))
        for line in network.branches:
            p, q, ell = cp.Variable(T), cp.Variable(T), cp.Variable(T, nonneg=True)
            r, x = line.r_pu / mg.base_mva, line.x_pu / mg.base_mva
            parent_v = voltage[line.parent] / line.tap_ratio**2
            constraints.append(
                ell <= line.s_max_mva**2 * line.tap_ratio**2 / mg.voltage_min_pu**2
            )
            constraints.extend(_capacity_polygon(p, q, line.s_max_mva, a.polygon_sides))
            constraints.extend(
                _capacity_polygon(
                    p - r * ell, q - x * ell, line.s_max_mva, a.polygon_sides
                )
            )
            # ||(2P,2Q,v-l)|| <= v+l is exactly P²+Q² <= vl.
            constraints.append(
                cp.SOC(
                    parent_v + ell, cp.vstack([2 * p, 2 * q, parent_v - ell]), axis=0
                )
            )
            constraints.append(
                voltage[line.child]
                == parent_v - 2 * (r * p + x * q) + (r * r + x * x) * ell
            )
            p_in[line.parent] -= p
            q_in[line.parent] -= q
            p_in[line.child] += p - r * ell
            q_in[line.child] += q - x * ell
            losses.append(r * ell)
        for bus in mg.buses:
            for active, reactive in (
                (self.p_wind, self.q_wind),
                (self.p_pv, self.q_pv),
            ):
                if bus in active:
                    p_in[bus] += active[bus]
                    q_in[bus] += reactive[bus]
            if bus == mg.storage.bus:
                p_in[bus] += self.p_discharge - self.p_charge
                q_in[bus] += self.q_storage
            if bus == mg.svg_bus:
                q_in[bus] += self.q_svg
            constraints.extend(
                [
                    p_in[bus] == self.network_load_p[bus],
                    q_in[bus] == self.network_load_q[bus],
                ]
            )
        self.network_loss = sum(losses, cp.Constant(np.zeros(T)))
        self.network_cost = a.dt_hours * (
            a.loss_value_cny_per_mwh * cp.sum(self.network_loss)
            + a.voltage_deviation_cost_cny_per_pu2h * sum(v_cost, cp.Constant(0.0))
        )

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
        objective_value = self.local_cost_expression.value
        if objective_value is None:
            raise RuntimeError(f"{self.microgrid.name} returned no objective value")
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
            loss_mw=np.asarray(self.network_loss.value, dtype=float)
            if self.network_enabled
            else self.p_loss_allowance_mw,
            dt_hours=a.dt_hours,
            rates=CostRates(
                a.curtailment_cost_cny_per_mwh,
                a.storage_degradation_cny_per_mwh,
                a.loss_value_cny_per_mwh,
            ),
        )
        return RegionalSchedule(
            name=self.microgrid.name,
            p_grid_mw=np.array(self.p_grid.value, dtype=float, copy=True),
            q_grid_mvar=np.array(self.q_grid.value, dtype=float, copy=True),
            renewable_used_mw=np.array(self.p_renew.value, dtype=float, copy=True),
            storage_charge_mw=np.array(self.p_charge.value, dtype=float, copy=True),
            storage_discharge_mw=np.array(
                self.p_discharge.value, dtype=float, copy=True
            ),
            storage_energy_mwh=np.array(self.energy.value, dtype=float, copy=True),
            q_support_mvar=np.array(self.q_support.value, dtype=float, copy=True),
            local_objective_cny=float(np.array(objective_value).item()),
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
        network_candidates: Mapping[str, RegionalNetworkCandidate] | None = None,
    ) -> None:
        self.case = case
        self.microgrids = list(microgrids)
        self.config = config
        self.M = len(self.microgrids)
        self.T = len(case.time_hours)
        self.p_grid_security_floors_mw = p_grid_security_floors_mw
        self.network_candidates = network_candidates
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
        margins = self.config.aggregate_pf_margin_mvar or (0.0,) * T
        if len(margins) != T:
            raise ValueError("aggregate PF margins must cover this planning window")
        pf_margin = np.asarray(margins, dtype=float)
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
        self.lower = cp.Parameter((M, T), value=lower.copy())
        constraints = [
            self.z_p >= self.lower,
            self.z_p <= upper,
            aggregate_p <= self.case.cluster_import_limit_mw,
            aggregate_q <= pf_tan * aggregate_p - pf_margin,
            -aggregate_q <= pf_tan * aggregate_p - pf_margin,
            self.peak >= aggregate_p,
        ]
        if self.network_candidates is not None:
            constraints.extend(
                [
                    self.z_p
                    == np.asarray(
                        [self.network_candidates[m.name].p_mw for m in self.microgrids]
                    ),
                    self.z_q
                    == np.asarray(
                        [
                            self.network_candidates[m.name].q_mvar
                            for m in self.microgrids
                        ]
                    ),
                ]
            )
        objective = (
            0.5
            * self.config.rho
            * (
                cp.sum_squares(self.z_p - self.v_p)
                + cp.sum_squares(self.z_q - self.v_q)
            )
            + a.cluster_peak_cost_cny_per_mw * self.peak
            + (
                a.cluster_ramp_cost_cny_per_mw * cp.norm1(cp.diff(aggregate_p))
                if T > 1
                else 0.0
            )
        )
        self.problem = cp.Problem(cp.Minimize(objective), constraints)
        if not self.problem.is_dpp():
            raise ValueError("cluster parameterized model must be DPP")

    def update_window(self, lower: Mapping[str, np.ndarray]) -> None:
        """Update floors after workspace structural validation."""
        self.lower.value = np.vstack([lower[mg.name] for mg in self.microgrids]).copy()

    def project(
        self, v_p: np.ndarray, v_q: np.ndarray
    ) -> tuple[np.ndarray, np.ndarray]:
        self.v_p.value = v_p
        self.v_q.value = v_q
        _solve_problem(self.problem, self.config.solver)
        if self.z_p.value is None or self.z_q.value is None:
            raise RuntimeError("cluster projection returned no solution")
        return np.array(self.z_p.value, dtype=float, copy=True), np.array(
            self.z_q.value, dtype=float, copy=True
        )
