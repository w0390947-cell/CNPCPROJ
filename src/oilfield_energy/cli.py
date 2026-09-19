"""命令行入口。"""

from __future__ import annotations

import argparse
from dataclasses import asdict, replace
from pathlib import Path

from .ac_consistency import solve_case_ac_consistent
from .ac_power_flow import validate_ac_dispatch
from .admm import run_admm_coordination
from .analysis import (
    compare_results,
    validate_result,
    write_case_data,
    write_summary,
    write_timeseries_csv,
)
from .data import build_synthetic_case
from .field_data import write_field_data_audit
from .group_control_scenarios import (
    run_group_control_scenarios,
    write_group_control_scenario_outputs,
)
from .hierarchical import run_hierarchical_control
from .hierarchy_reporting import write_fault_demo_outputs, write_hierarchical_outputs
from .hierarchy_types import (
    ADMMConfig,
    CommunicationConfig,
    GroupControlConfig,
    TimeScaleConfig,
)
from .model import solve_case
from .network_model import NetworkContingency
from .network_scenarios import (
    NetworkOperatingPoint,
    NetworkSecurityLimits,
    build_network_scenarios,
    evaluate_network_scenarios,
    write_network_scenario_outputs,
)
from .plotting import plot_cluster, plot_microgrid


def _require_success(label, result) -> None:
    if not result.success:
        raise RuntimeError(f"{label} failed: status={result.status}, message={result.message}")


def run(steps: int, output: Path, time_limit: float, legacy_milp: bool = False) -> None:
    case = build_synthetic_case(steps=steps)
    single_names = ["SC"]
    cluster_names = [mg.name for mg in case.microgrids]
    exactness = {}

    def solve_dispatch(label: str, names, *, storage_enabled: bool, cluster_coordination: bool):
        if legacy_milp:
            return solve_case(
                case, names,
                storage_enabled=storage_enabled,
                cluster_coordination=cluster_coordination,
                time_limit_seconds=time_limit,
            )
        checked = solve_case_ac_consistent(
            case, names,
            storage_enabled=storage_enabled,
            cluster_coordination=cluster_coordination,
            time_limit_seconds=time_limit,
        )
        exactness[label] = {
            "passed": checked.passed,
            "stop_reason": checked.stop_reason,
            "iterations": checked.iterations,
            "history": [asdict(item) for item in checked.history],
        }
        if not checked.passed:
            raise RuntimeError(f"{label} failed AC consistency: {checked.stop_reason}")
        return checked.optimization

    print("[1/4] Solving single-microgrid baseline...")
    single_baseline = solve_dispatch(
        "single_baseline", single_names,
        storage_enabled=False, cluster_coordination=False,
    )
    _require_success("single baseline", single_baseline)
    print("[2/4] Solving single-microgrid optimized case...")
    single_optimized = solve_dispatch(
        "single_optimized", single_names,
        storage_enabled=True, cluster_coordination=False,
    )
    _require_success("single optimized", single_optimized)
    print("[3/4] Solving three-microgrid baseline...")
    cluster_baseline = solve_dispatch(
        "cluster_baseline", cluster_names,
        storage_enabled=False, cluster_coordination=True,
    )
    _require_success("cluster baseline", cluster_baseline)
    print("[4/4] Solving three-microgrid coordinated case...")
    cluster_optimized = solve_dispatch(
        "cluster_optimized", cluster_names,
        storage_enabled=True, cluster_coordination=True,
    )
    _require_success("cluster optimized", cluster_optimized)

    output.mkdir(parents=True, exist_ok=True)
    single_comparison = compare_results(single_baseline, single_optimized)
    cluster_comparison = compare_results(cluster_baseline, cluster_optimized)
    payload = {
        "case_notice": "全部网络与运行数据均为参数化模拟数据，不代表长庆油田真实数据。",
        "economic_claim": {
            "comparison_scope": "synthetic_no_storage_optimization_reference",
            "formal_ten_percent_requirement_certified": False,
            "reason": (
                "当前基准关闭储能且仍优化其他资源，不等同于山城现行分时就地策略；"
                "正式指标必须使用同一真实数据、已确认成本口径和现行策略回放。"
            ),
        },
        "network_formulation": "legacy LinDistFlow MILP" if legacy_milp else "AC-consistent Branch Flow MISOCP",
        "ac_consistency": exactness,
        "steps": steps,
        "dt_hours": case.assumptions.dt_hours,
        "single_microgrid": {
            "comparison": single_comparison,
            "validation": validate_result(case, single_optimized, single_names),
            "ac_power_flow_validation": validate_ac_dispatch(case, single_optimized, single_names),
            "model_size": single_optimized.model_size,
            "solver_message": single_optimized.message,
            "mip_gap": single_optimized.mip_gap,
        },
        "multi_microgrid": {
            "microgrid_count": len(cluster_names),
            "comparison": cluster_comparison,
            "validation": validate_result(case, cluster_optimized, cluster_names),
            "ac_power_flow_validation": validate_ac_dispatch(case, cluster_optimized, cluster_names),
            "model_size": cluster_optimized.model_size,
            "solver_message": cluster_optimized.message,
            "mip_gap": cluster_optimized.mip_gap,
        },
    }
    write_summary(output / "summary.json", payload)
    write_case_data(output / "synthetic_case.json", case)
    write_timeseries_csv(output / "single_timeseries.csv", case, single_optimized, single_baseline, single_names)
    write_timeseries_csv(output / "cluster_timeseries.csv", case, cluster_optimized, cluster_baseline, cluster_names)
    plot_microgrid(output / "single_dispatch.png", case, single_optimized, single_baseline, "SC")
    plot_cluster(output / "cluster_dispatch.png", case, cluster_optimized, cluster_baseline, cluster_names)
    print(
        "Single-microgrid synthetic no-storage reference difference: "
        f"{single_comparison['economic_improvement_percent']:.2f}%"
    )
    print(
        "Cluster synthetic no-storage reference difference: "
        f"{cluster_comparison['economic_improvement_percent']:.2f}%"
    )
    print(f"Results written to: {output.resolve()}")


def run_hierarchy(
    steps: int,
    output: Path,
    time_limit: float,
    include_fault_demo: bool,
) -> None:
    """运行三模型多层级控制，并导出可审阅结果。"""
    case = build_synthetic_case(steps=steps)
    names = [mg.name for mg in case.microgrids]
    admm_config = ADMMConfig()
    communication_config = CommunicationConfig()
    time_scale_config = TimeScaleConfig()
    group_control_config = GroupControlConfig()
    print("[1/5] Solving AC-consistent centralized MISOCP and convex ADMM coordination...")
    print("[2/5] Solving independent AC-consistent regional MISOCP realizations...")
    print("[3/5] Simulating intraday update and one-minute device tracking...")
    result = run_hierarchical_control(
        case,
        names,
        admm_config=admm_config,
        communication_config=communication_config,
        time_scale_config=time_scale_config,
        group_control_config=group_control_config,
        time_limit_seconds=time_limit,
    )
    print("[4/5] Running deterministic group-control scenario regression...")
    group_scenarios = run_group_control_scenarios(config=group_control_config)
    failed_scenarios = [
        name for name, scenario in group_scenarios.items() if not scenario.passed
    ]
    result.comparison["group_control_scenarios_passed"] = not failed_scenarios
    result.comparison["group_control_scenario_count"] = float(len(group_scenarios))
    write_hierarchical_outputs(
        output,
        case,
        names,
        result,
        configuration={
            "steps": steps,
            "dt_hours": case.assumptions.dt_hours,
            "admm": vars(admm_config),
            "communication": vars(communication_config),
            "time_scales": vars(time_scale_config),
            "group_control": vars(group_control_config),
        },
    )
    write_group_control_scenario_outputs(
        output / "group_control_scenarios",
        group_scenarios,
    )
    # Preserve diagnostic trajectories before refusing a safety certification.
    if not result.comparison["overall_passed"]:
        raise RuntimeError("hierarchical execution not certified; inspect saved stage assessments")
    if failed_scenarios:
        raise RuntimeError(
            "group-control scenario regression failed: "
            + ", ".join(failed_scenarios)
        )

    if include_fault_demo:
        print("[5/5] Running reduced-size delay/loss/outage recovery regression...")
        fault_case = build_synthetic_case(steps=min(24, steps))
        fault_config = CommunicationConfig(
            loss_probability=0.05,
            min_delay_iterations=0,
            max_delay_iterations=2,
            stale_limit_iterations=3,
            outage_region="YA_B",
            outage_start_iteration=12,
            outage_end_iteration=22,
        )
        fault_result = run_admm_coordination(
            fault_case,
            admm_config=ADMMConfig(max_iterations=450),
            communication_config=fault_config,
        )
        if not fault_result.converged:
            raise RuntimeError("fault-regression ADMM did not recover before its iteration limit")
        write_fault_demo_outputs(
            output / "fault_demo",
            fault_case,
            fault_result,
            configuration={
                "admm": vars(ADMMConfig(max_iterations=450)),
                "communication": vars(fault_config),
            },
        )
    else:
        print("[5/5] Communication fault regression skipped by command-line option.")

    print(f"ADMM convergence: {result.admm.coordination_updates} coordination updates")
    print(f"Distributed-vs-centralized economic gap: {result.comparison['distributed_cost_gap_percent']:.2f}%")
    print(f"Device safety passed: {result.comparison['device_safety_passed']}")
    print(f"Results written to: {output.resolve()}")


def run_group_scenario_suite(output: Path) -> None:
    """单独运行群调群控确定性场景集，不启动MISOCP或ADMM。"""
    results = run_group_control_scenarios()
    write_group_control_scenario_outputs(output, results)
    failed = [name for name, result in results.items() if not result.passed]
    if failed:
        raise RuntimeError("group-control scenarios failed: " + ", ".join(failed))
    print(f"Group-control scenarios passed: {len(results)}/{len(results)}")
    print(f"Results written to: {output.resolve()}")


def run_field_audit(
    output: Path,
    short_circuit_workbook: Path,
    line_load_workbook: Path,
) -> None:
    """只读审计项目方工作簿，输出数据质量和现场算例就绪性报告。"""
    target = output / "field_data_audit.json"
    payload = write_field_data_audit(
        target,
        short_circuit_workbook=short_circuit_workbook,
        line_load_workbook=line_load_workbook,
    )
    print(f"Field-data status: {payload['status']}")
    print(f"Audit written to: {target.resolve()}")


def run_network_scenario_suite(steps: int, output: Path, time_limit: float) -> None:
    """显式合成算例静态N−1演示；完整归档预期的径向孤岛失败。"""
    case = build_synthetic_case(steps=steps)
    names = [mg.name for mg in case.microgrids]
    checked = solve_case_ac_consistent(case, names, time_limit_seconds=time_limit)
    if not checked.passed:
        raise RuntimeError(f"network scenario base dispatch failed: {checked.stop_reason}")
    summaries = {}
    for mg in case.microgrids:
        # 只给标记为 synthetic 的演示算例生成单支路故障；真实台账由甲方定义。
        network = mg.network_model_v2
        if network is None or not network.provenance.synthetic:
            raise ValueError("this command requires an explicit synthetic network")
        network = replace(network, contingencies=tuple(
            NetworkContingency(f"N-1:{branch.branch_id}", (branch.branch_id,),
                               provenance="synthetic branch outage screening")
            for branch in network.branches
        ))
        dispatch = checked.optimization.microgrids[mg.name]
        points = tuple(NetworkOperatingPoint(
            point_id=f"t={float(hour):.9g}h",
            source="synthetic AC-consistent optimized net bus demand",
            p_demand_mw_by_bus={bus: float(dispatch["bus_p_demand_mw"][i, t]) for i, bus in enumerate(mg.buses)},
            q_demand_mvar_by_bus={bus: float(dispatch["bus_q_demand_mvar"][i, t]) for i, bus in enumerate(mg.buses)},
            quality_valid=True, coherent=True,  # 显式合成计划快照，不代表现场质量/时效判定。
        ) for t, hour in enumerate(case.time_hours))
        batch = evaluate_network_scenarios(
            network, points, build_network_scenarios(network), NetworkSecurityLimits(
                mg.voltage_min_pu, mg.voltage_max_pu,
                max(mg.p_grid_min_mw, case.assumptions.no_reverse_margin_mw),
                mg.p_grid_max_mw, case.assumptions.pf_min,
            ),
        )
        write_network_scenario_outputs(output / mg.name, batch, model=network, points=points)
        summaries[mg.name] = {
            "scenario_count": len(batch.results),
            "point_count": len(points),
            "all_final_states_secure": batch.all_final_states_secure,
            "n_minus_one_coverage_complete": batch.n_minus_one_coverage_complete,
            "results": {item.scenario.scenario_id: item.status.value for item in batch.results},
        }
        print(f"{mg.name}: {len(batch.results)} scenarios; final states secure={batch.all_final_states_secure}")
    write_summary(output / "summary.json", {
        "scope": "synthetic_fixed_injection_static_network_screening",
        "field_acceptance_certified": False,
        "notice": "径向网络断线导致孤岛是校核发现，不会被标成N−1通过。未定义转供方式不自动补造联络线。",
        "microgrids": summaries,
    })
    print(f"Network scenario reports written to: {output.resolve()}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Oilfield source-grid-load-storage optimization")
    parser.add_argument(
        "command",
        nargs="?",
        default="run",
        choices=["run", "hierarchical", "group-scenarios", "field-audit", "network-scenarios", "compare-power-flow", "field-power-flow"],
    )
    parser.add_argument("--steps", type=int, default=96, help="24-hour time steps (96 = 15 min)")
    parser.add_argument("--output", type=Path, default=Path("results"))
    parser.add_argument("--input", type=Path, help="JSON request for compare-power-flow or field-power-flow")
    parser.add_argument("--at", help="field-power-flow target ISO timestamp with timezone")
    parser.add_argument("--demo", action="store_true", help="explicitly allow synthetic field-power-flow demo inputs")
    parser.add_argument("--time-limit", type=float, default=180.0, help="seconds per MILP")
    parser.add_argument(
        "--skip-fault-demo", action="store_true",
        help="skip the reduced-size communication delay/loss/outage regression",
    )
    parser.add_argument(
        "--legacy-milp", action="store_true",
        help="use the former approximate LinDistFlow MILP instead of the default AC-consistent MISOCP",
    )
    parser.add_argument(
        "--short-circuit-workbook", type=Path,
        default=Path("母线容量、阻抗.xlsx"),
        help="project-party bus short-circuit workbook",
    )
    parser.add_argument(
        "--line-load-workbook", type=Path,
        default=Path("线路负荷统计0901.xlsx"),
        help="project-party line/load history workbook",
    )
    args = parser.parse_args()
    if args.command == "field-power-flow":
        if args.input is None:
            parser.error("field-power-flow requires --input")
        from .field_data.snapshot_power_flow import run_field_power_flow_file
        raise SystemExit(run_field_power_flow_file(args.input, args.output, demo=args.demo, at=args.at))
    elif args.command == "compare-power-flow":
        if args.input is None:
            parser.error("compare-power-flow requires --input")
        from .power_flow_comparison import run_comparison_file
        try:
            exit_code = run_comparison_file(args.input, args.output)
        except (ValueError, OSError) as exc:
            parser.exit(2, f"Fixed-state comparison input/output error: {exc}\n")
        raise SystemExit(exit_code)
    elif args.command == "hierarchical":
        run_hierarchy(args.steps, args.output, args.time_limit, not args.skip_fault_demo)
    elif args.command == "network-scenarios":
        run_network_scenario_suite(args.steps, args.output, args.time_limit)
    elif args.command == "group-scenarios":
        run_group_scenario_suite(args.output)
    elif args.command == "field-audit":
        run_field_audit(
            args.output,
            args.short_circuit_workbook,
            args.line_load_workbook,
        )
    else:
        run(args.steps, args.output, args.time_limit, args.legacy_milp)


if __name__ == "__main__":
    main()
