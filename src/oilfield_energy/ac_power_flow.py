"""独立的平衡三相径向 AC 潮流校核。

本模块用前推回代法重新计算复电压、电流、PCC复功率和 I²R 网损，
供 MISOCP/旧 MILP 调度复核及多运行方式、故障场景校核共用。
"""

from __future__ import annotations

from typing import Dict, Mapping, Sequence

import numpy as np

from .data import MicrogridData, ProjectCase
from .model import OptimizationResult
from .modules.power_flow.api import assess_power_balance, constant_power_current
from .modules.power_flow.contracts import FlowNumerics, RadialBalanceBranch, SingularPowerVoltage
from .network_model import NetworkPhaseModel, ResolvedNetwork, validate_legacy_network_alignment


def backward_forward_sweep(
    mg: MicrogridData,
    p_demand_mw: np.ndarray,
    q_demand_mvar: np.ndarray,
    *,
    slack_voltage_pu: float = 1.0,
    tolerance: float = 1e-10,
    max_iterations: int = 100,
    power_tolerance_pu: float = 1e-8,
    singular_voltage_pu: float = 1e-8,
) -> Dict[str, object]:
    """求解一个时刻的径向恒功率潮流。正需求表示负荷，负需求表示发电。"""
    network = validate_legacy_network_alignment(mg)
    return backward_forward_sweep_resolved(
        network,
        p_demand_mw,
        q_demand_mvar,
        slack_voltage_pu=slack_voltage_pu,
        tolerance=tolerance,
        max_iterations=max_iterations,
        power_tolerance_pu=power_tolerance_pu,
        singular_voltage_pu=singular_voltage_pu,
    )


def backward_forward_sweep_resolved(
    network: ResolvedNetwork,
    p_demand_mw: np.ndarray,
    q_demand_mvar: np.ndarray,
    *,
    slack_voltage_pu: float = 1.0,
    tolerance: float = 1e-10,
    max_iterations: int = 100,
    power_tolerance_pu: float = 1e-8,
    singular_voltage_pu: float = 1e-8,
) -> Dict[str, object]:
    """计算已解析径向网络；输入按 network.buses 排序且不含固定并联补偿。

    该入口不依赖单储能/单SVG的旧 MicrogridData，可用于纯网络场景。
    """
    if isinstance(slack_voltage_pu, (bool, str)) or not np.isfinite(slack_voltage_pu) or slack_voltage_pu <= 0:
        raise ValueError("slack_voltage_pu must be a finite positive voltage magnitude")
    numerics = FlowNumerics(tolerance, power_tolerance_pu, singular_voltage_pu)
    if type(max_iterations) is not int or max_iterations < 1:
        raise ValueError("max_iterations must be a positive integer")
    buses = tuple(bus.bus_id for bus in network.buses)
    lines = network.branches
    n_bus = len(buses)
    p = np.asarray(p_demand_mw, dtype=float)
    q = np.asarray(q_demand_mvar, dtype=float)
    if p.shape != (n_bus,) or q.shape != (n_bus,):
        raise ValueError("P/Q demand must contain one value for every network bus")
    if not np.all(np.isfinite(p)) or not np.all(np.isfinite(q)):
        raise ValueError("P/Q demand must be finite")
    if network.phase_model is not NetworkPhaseModel.BALANCED_POSITIVE_SEQUENCE:
        raise ValueError("only balanced positive-sequence networks are supported")
    visited = {network.pcc_bus_id}
    if network.pcc_bus_id not in buses or len(set(buses)) != n_bus:
        raise ValueError("invalid resolved bus/PCC identity")
    for line in lines:
        if line.parent not in visited or line.child in visited or line.child not in buses:
            raise ValueError("resolved branches must be a topologically ordered rooted tree")
        if not np.isclose(line.phase_shift_degrees, 0.0, rtol=0.0, atol=1e-12):
            raise ValueError("phase-shifting transformers are not supported")
        visited.add(line.child)
    if visited != set(buses):
        raise ValueError("resolved network is disconnected")
    bus_index = {bus: idx for idx, bus in enumerate(buses)}
    slack = bus_index[network.pcc_bus_id]
    base_mva = network.base_mva
    shunt_q_nominal = np.asarray(
        [network.shunt_q_nominal_mvar_by_bus[bus] for bus in buses],
        dtype=float,
    )
    # p/q_demand 只含负荷与可控设备的净恒功率需求。固定并联补偿属于
    # 网络台账，按恒导纳模型 Q=Q_nominal*|V|² 单独形成电流。
    s_pu = (p + 1j * q) / base_mva
    voltage = np.full(n_bus, complex(slack_voltage_pu), dtype=complex)
    branch_current = np.zeros(len(lines), dtype=complex)
    converged = False
    stop_reason = "MAX_ITERATIONS"
    power_residual_pu = None
    accumulated = np.zeros(n_bus, dtype=complex)
    balance_branches = tuple(
        RadialBalanceBranch(
            bus_index[line.parent],
            bus_index[line.child],
            complex(line.r_pu, line.x_pu),
            line.tap_ratio,
        )
        for line in lines
    )

    for iteration in range(1, max_iterations + 1):
        old_voltage = voltage.copy()
        try:
            bus_current = constant_power_current(s_pu, voltage, numerics.singular_voltage_pu)
        except SingularPowerVoltage:
            stop_reason = "SINGULAR_CONSTANT_POWER_VOLTAGE"
            break
        bus_current += 1j * (shunt_q_nominal / base_mva) * voltage
        accumulated = bus_current.copy()
        for line_idx in range(len(lines) - 1, -1, -1):
            line = lines[line_idx]
            child = bus_index[line.child]
            parent = bus_index[line.parent]
            branch_current[line_idx] = accumulated[child]
            tap = line.tap_ratio * np.exp(1j * np.deg2rad(line.phase_shift_degrees))
            accumulated[parent] += branch_current[line_idx] / np.conj(tap)

        voltage[slack] = complex(slack_voltage_pu)
        for line_idx, line in enumerate(lines):
            parent = bus_index[line.parent]
            child = bus_index[line.child]
            z = line.r_pu + 1j * line.x_pu
            tap = line.tap_ratio * np.exp(1j * np.deg2rad(line.phase_shift_degrees))
            voltage[child] = voltage[parent] / tap - z * branch_current[line_idx]
        if float(np.max(np.abs(voltage - old_voltage))) <= tolerance:
            # Small changes alone cannot establish a constant-power solution.
            try:
                constant_power_current(s_pu, voltage, numerics.singular_voltage_pu)
                residual = assess_power_balance(
                    voltage,
                    s_pu,
                    shunt_q_nominal / base_mva,
                    balance_branches,
                    slack,
                )
            except SingularPowerVoltage:
                stop_reason = "SINGULAR_CONSTANT_POWER_VOLTAGE"
                break
            power_residual_pu = residual.maximum_pu
            if power_residual_pu <= numerics.power_balance_tolerance_pu:
                converged = True
                stop_reason = "CONVERGED"
                break
            stop_reason = "POWER_BALANCE_RESIDUAL"

    root_current = accumulated[slack]
    pcc_power_mva = voltage[slack] * np.conj(root_current) * base_mva
    loss_mw = float(sum(
        line.r_pu * abs(branch_current[idx]) ** 2 * base_mva
        for idx, line in enumerate(lines)
    ))
    reactive_loss_mvar = float(sum(
        line.x_pu * abs(branch_current[idx]) ** 2 * base_mva
        for idx, line in enumerate(lines)
    ))
    loading = np.zeros(len(lines))
    sending_p = np.zeros(len(lines))
    sending_q = np.zeros(len(lines))
    receiving_p = np.zeros(len(lines))
    receiving_q = np.zeros(len(lines))
    current_squared_mva2 = np.zeros(len(lines))
    p_loss_line = np.zeros(len(lines))
    q_loss_line = np.zeros(len(lines))
    for idx, line in enumerate(lines):
        tap = line.tap_ratio * np.exp(1j * np.deg2rad(line.phase_shift_degrees))
        sending_voltage = voltage[bus_index[line.parent]] / tap
        sending_power = sending_voltage * np.conj(branch_current[idx]) * base_mva
        receiving_power = voltage[bus_index[line.child]] * np.conj(branch_current[idx]) * base_mva
        sending_p[idx] = float(np.real(sending_power))
        sending_q[idx] = float(np.imag(sending_power))
        receiving_p[idx] = float(np.real(receiving_power))
        receiving_q[idx] = float(np.imag(receiving_power))
        current_squared_mva2[idx] = abs(branch_current[idx]) ** 2 * base_mva ** 2
        p_loss_line[idx] = line.r_pu * current_squared_mva2[idx] / base_mva
        q_loss_line[idx] = line.x_pu * current_squared_mva2[idx] / base_mva
        loading[idx] = max(abs(sending_power), abs(receiving_power)) / line.s_max_mva
    return {
        "converged": converged,
        "physical_values_valid": converged,
        "stop_reason": stop_reason,
        "maximum_power_balance_residual_pu": power_residual_pu,
        "power_tolerance_pu": numerics.power_balance_tolerance_pu,
        "singular_voltage_pu": numerics.singular_voltage_pu,
        "voltage_step_tolerance_pu": numerics.voltage_step_tolerance_pu,
        "iterations": iteration,
        "slack_voltage_pu": float(slack_voltage_pu),
        "voltage_pu": np.abs(voltage),
        "pcc_p_mw": float(np.real(pcc_power_mva)),
        "pcc_q_mvar": float(np.imag(pcc_power_mva)),
        "loss_mw": loss_mw,
        "reactive_loss_mvar": reactive_loss_mvar,
        "line_sending_p_mw": sending_p,
        "line_sending_q_mvar": sending_q,
        "line_receiving_p_mw": receiving_p,
        "line_receiving_q_mvar": receiving_q,
        "line_current_squared_mva2": current_squared_mva2,
        "line_p_loss_mw": p_loss_line,
        "line_q_loss_mvar": q_loss_line,
        "line_loading_pu": loading,
        "line_ids": tuple(line.branch_id for line in lines),
        "line_parent_bus_ids": tuple(line.parent for line in lines),
        "line_child_bus_ids": tuple(line.child for line in lines),
        "line_tap_ratios": np.asarray([line.tap_ratio for line in lines]),
        "network_operating_mode_id": network.operating_mode_id,
        "network_contingency_id": network.contingency_id,
        "fixed_shunt_q_nominal_mvar_by_bus": dict(network.shunt_q_nominal_mvar_by_bus),
        "fixed_shunt_q_mvar_by_bus": {
            bus: network.shunt_q_nominal_mvar_by_bus[bus] * abs(voltage[bus_index[bus]]) ** 2
            for bus in buses
        },
    }


def validate_ac_dispatch(
    case: ProjectCase,
    result: OptimizationResult,
    microgrid_names: Sequence[str],
) -> Dict[str, object]:
    lookup = {mg.name: mg for mg in case.microgrids}
    report: Dict[str, object] = {"microgrids": {}}
    passed = True
    for name in microgrid_names:
        mg = lookup[name]
        network = validate_legacy_network_alignment(mg)
        lines = network.branches
        dispatch = result.microgrids[name]
        p_bus = np.asarray(dispatch["bus_p_demand_mw"])
        q_bus = np.asarray(dispatch["bus_q_demand_mvar"])
        T = p_bus.shape[1]
        voltages = np.zeros((len(mg.buses), T))
        loading = np.zeros((len(lines), T))
        pcc_p = np.zeros(T)
        pcc_q = np.zeros(T)
        losses = np.zeros(T)
        reactive_losses = np.zeros(T)
        line_p_losses = np.zeros((len(lines), T))
        line_q_losses = np.zeros((len(lines), T))
        line_current_squared = np.zeros((len(lines), T))
        converged = np.ones(T, dtype=bool)
        for t in range(T):
            flow = backward_forward_sweep(mg, p_bus[:, t], q_bus[:, t])
            converged[t] = bool(flow["converged"])
            voltages[:, t] = np.asarray(flow["voltage_pu"])
            loading[:, t] = np.asarray(flow["line_loading_pu"])
            pcc_p[t] = float(flow["pcc_p_mw"])
            pcc_q[t] = float(flow["pcc_q_mvar"])
            losses[t] = float(flow["loss_mw"])
            reactive_losses[t] = float(flow["reactive_loss_mvar"])
            line_p_losses[:, t] = np.asarray(flow["line_p_loss_mw"])
            line_q_losses[:, t] = np.asarray(flow["line_q_loss_mvar"])
            line_current_squared[:, t] = np.asarray(flow["line_current_squared_mva2"])
        apparent = np.sqrt(pcc_p ** 2 + pcc_q ** 2)
        pf = np.divide(np.abs(pcc_p), apparent, out=np.ones_like(pcc_p), where=apparent > 1e-9)
        security_floor = np.asarray(
            dispatch.get(
                "p_grid_security_floor_mw",
                np.full(
                    T,
                    max(mg.p_grid_min_mw, case.assumptions.no_reverse_margin_mw),
                ),
            ),
            dtype=float,
        )
        if security_floor.shape != (T,):
            raise ValueError(f"{name} dispatch security floor must contain {T} time steps")
        security_headroom = pcc_p - security_floor
        # 网络净注入可行不代表设备计划可行；逐资源独立核对MW额定值和预测上界。
        renewable_violations: list[str] = []
        for kind, available, rated in (
            ("wind", mg.wind_available_mw, mg.wind_capacity_mw),
            ("pv", mg.pv_available_mw, mg.pv_capacity_mw),
        ):
            schedules = dispatch.get(f"{kind}_active_by_bus_mw", {})
            if not isinstance(schedules, Mapping) or set(schedules) != set(available):
                renewable_violations.append(f"{kind}:missing_or_unexpected_resource")
                continue
            for bus, forecast in available.items():
                values = np.asarray(schedules[bus], dtype=float)
                if values.shape != (T,) or not np.all(np.isfinite(values)):
                    renewable_violations.append(f"{kind}:{bus}:invalid_schedule")
                    continue
                if np.any(values < -1e-6):
                    renewable_violations.append(f"{kind}:{bus}:negative_active_power")
                capacity = rated.get(bus, float("nan"))
                if not np.isfinite(capacity) or capacity < 0 or np.any(values > capacity + 1e-6):
                    renewable_violations.append(f"{kind}:{bus}:rated_capacity_exceeded_or_invalid")
                forecast_values = np.asarray(forecast, dtype=float)
                if (
                    forecast_values.shape != (T,)
                    or not np.all(np.isfinite(forecast_values))
                    or np.any(forecast_values < 0)
                    or np.any(values > forecast_values + 1e-6)
                ):
                    renewable_violations.append(f"{kind}:{bus}:availability_exceeded_or_invalid")
        local = {
            "all_converged": bool(np.all(converged)),
            "no_reverse": bool(np.min(pcc_p) >= -1e-6),
            "planning_security_floor_compliant": bool(np.min(security_headroom) >= -1e-6),
            "pf_compliant": bool(np.min(pf) >= case.assumptions.pf_min - 1e-6),
            "voltage_compliant": bool(
                np.min(voltages) >= mg.voltage_min_pu - 1e-6
                and np.max(voltages) <= mg.voltage_max_pu + 1e-6
            ),
            "line_capacity_compliant": bool(np.max(loading) <= 1.0 + 1e-6),
            "renewable_active_power_compliant": not renewable_violations,
            "renewable_active_power_violations": renewable_violations,
            "minimum_pcc_import_mw": float(np.min(pcc_p)),
            "minimum_planning_security_floor_mw": float(np.min(security_floor)),
            "maximum_planning_security_floor_mw": float(np.max(security_floor)),
            "minimum_planning_security_headroom_mw": float(np.min(security_headroom)),
            "minimum_power_factor": float(np.min(pf)),
            "minimum_voltage_pu": float(np.min(voltages)),
            "maximum_voltage_pu": float(np.max(voltages)),
            "maximum_line_loading_pu": float(np.max(loading)),
            "total_loss_mwh": float(np.sum(losses) * case.assumptions.dt_hours),
            "total_reactive_loss_mvarh": float(
                np.sum(reactive_losses) * case.assumptions.dt_hours
            ),
            "maximum_pcc_p_difference_from_linear_mw": float(
                np.max(np.abs(pcc_p - np.asarray(dispatch["p_grid_mw"])))
            ),
            "maximum_pcc_q_difference_from_optimization_mvar": float(
                np.max(np.abs(pcc_q - np.asarray(dispatch["q_grid_mvar"])))
            ),
            "maximum_voltage_difference_from_optimization_pu": float(
                np.max(np.abs(voltages - np.asarray(dispatch["voltage_pu"])))
            ),
        }
        if "line_p_loss_mw" in dispatch:
            local["maximum_line_p_loss_difference_mw"] = float(np.max(np.abs(
                line_p_losses - np.asarray(dispatch["line_p_loss_mw"])
            )))
            local["maximum_line_q_loss_difference_mvar"] = float(np.max(np.abs(
                line_q_losses - np.asarray(dispatch["line_q_loss_mvar"])
            )))
            local["maximum_line_current_squared_difference_mva2"] = float(np.max(np.abs(
                line_current_squared - np.asarray(dispatch["line_current_squared_mva2"])
            )))
        local["passed"] = all(value for value in local.values() if isinstance(value, bool))
        passed = passed and bool(local["passed"])
        report["microgrids"][name] = local
    report["passed"] = passed
    return report
