"""命令行入口。"""

from __future__ import annotations

import argparse
from dataclasses import asdict, replace
from pathlib import Path

from .ac_consistency import solve_case_ac_consistent
from .ac_power_flow import validate_ac_dispatch
from .analysis import (
    compare_results,
    validate_result,
    write_case_data,
    write_summary,
    write_timeseries_csv,
)
from .bootstrap.adapters.cluster_study import run_cluster_batch, run_cluster_study
from .bootstrap.adapters.project_dataset import build_synthetic_case
from .bootstrap.adapters.single_execution import (
    prepare_single_case,
    run_single_execution,
    single_execution_policy,
)
from .field_data import write_field_data_audit
from .group_control_scenarios import (
    run_group_control_scenarios,
    scenario_basis,
    write_group_control_scenario_outputs,
)
from .modules.dispatch.api import assess_single_economic_requirement
from .network_model import NetworkContingency
from .network_scenarios import (
    NetworkOperatingPoint,
    NetworkSecurityLimits,
    build_network_scenarios,
    evaluate_network_scenarios,
    write_network_scenario_outputs,
)
from .plotting import plot_cluster, plot_microgrid
from .service import ScenarioType, SimulationRequest, SolverOptions


def _require_success(label, result) -> None:
    if not result.success:
        raise RuntimeError(f"{label} failed: status={result.status}, message={result.message}")


def run(steps: int, output: Path, time_limit: float) -> None:
    case = build_synthetic_case(steps=steps)
    single_names = ["SC"]
    cluster_names = [mg.name for mg in case.microgrids]
    exactness = {}
    single_policy = single_execution_policy()

    def solve_dispatch(label: str, names, *, storage_enabled: bool, cluster_coordination: bool):
        planning_case = case if cluster_coordination else prepare_single_case(
            case, names[0], storage_enabled=storage_enabled, policy=single_policy,
        )
        checked = solve_case_ac_consistent(
            planning_case, names,
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
    single_execution = run_single_execution(
        case, single_optimized, single_names[0], storage_enabled=True,
        time_limit_seconds=time_limit, packaged_inputs=True, events=[], policy=single_policy,
    )
    cluster_result = None if steps < 4 else run_cluster_study(
        SimulationRequest(
            name="Cluster rolling study", scenario_type=ScenarioType.CLUSTER_COORDINATION,
            steps=steps, solver=SolverOptions(time_limit_seconds=time_limit),
        ),
        output / "cluster_rolling",
    )
    payload = {
        "case_notice": "全部网络与运行数据均为参数化模拟数据，不代表长庆油田真实数据。",
        "economic_claim": {
            "comparison_scope": "synthetic_no_storage_optimization_reference",
            "formal_ten_percent_requirement_certified": assess_single_economic_requirement(
                float(single_comparison["baseline_economic_cost_cny"]),
                float(single_comparison["optimized_economic_cost_cny"]),
            )[1],
            "reason": (
                "按已确认口径比较无储能优化方案与选定方案的计划综合成本；"
                "本项独立于实际执行的安全、跟踪和运行成本评价。"
            ),
        },
        "network_formulation": "AC-consistent Branch Flow MISOCP",
        "ac_consistency": exactness,
        "steps": steps,
        "dt_hours": case.assumptions.dt_hours,
        "single_microgrid": {
            "unified_execution": single_execution.model_dump(mode="json") if single_execution else None,
            "unified_execution_passed": single_execution.status == "passed" if single_execution else False,
            "comparison": single_comparison,
            "validation": validate_result(case, single_optimized, single_names),
            "ac_power_flow_validation": validate_ac_dispatch(case, single_optimized, single_names),
            "model_size": single_optimized.model_size,
            "solver_message": single_optimized.message,
            "mip_gap": single_optimized.mip_gap,
        },
        "multi_microgrid": {
            "execution_scope": "cluster-rolling-v1" if cluster_result else "reference_only",
            "cluster_execution": cluster_result.cluster_execution.model_dump(mode="json")
            if cluster_result and cluster_result.cluster_execution else None,

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
    if cluster_result is not None and (
        cluster_result.cluster_execution is None or cluster_result.cluster_execution.status != "passed"
    ):
        raise RuntimeError("cluster rolling execution not certified; inspect saved execution evidence")
    if single_execution is not None and single_execution.status != "passed":
        raise RuntimeError("single-microgrid V2 execution not certified; inspect saved execution evidence")


def run_cluster(
    steps: int,
    output: Path,
    time_limit: float,
    *,
    request_file: Path | None = None,
    communication_fault: bool = False,
) -> None:
    """Compose the same request/service as the Web entry; save evidence before checking status."""
    request = (
        SimulationRequest.model_validate_json(request_file.read_bytes())
        if request_file is not None
        else SimulationRequest(
            name="Cluster rolling study",
            scenario_type=ScenarioType.COMMUNICATION_FAULT if communication_fault else ScenarioType.CLUSTER_COORDINATION,
            steps=steps,
            solver=SolverOptions(time_limit_seconds=time_limit),
        )
    )
    result = run_cluster_study(request, output)
    execution = result.cluster_execution
    print(f"Results written to: {output.resolve()}")
    if execution is None or execution.status != "passed":
        raise RuntimeError("cluster rolling execution not certified; inspect saved execution evidence")
    print(f"Algorithm: {execution.algorithm_version}; execution: {execution.status}")


def run_hierarchy(steps: int, output: Path, time_limit: float, include_fault_demo: bool) -> None:
    """Command alias only; the former Python algorithm and result format were retired."""
    print("hierarchical now calls cluster-rolling-v1; exports SimulationResult, not the former hierarchical report.")
    run_cluster(steps, output, time_limit)
    if include_fault_demo:
        run_cluster(steps, output / "fault_demo", time_limit, communication_fault=True)


def run_group_scenario_suite(output: Path) -> None:
    """单独运行群调群控确定性场景集，不启动MISOCP或ADMM。"""
    results = run_group_control_scenarios(basis=scenario_basis(build_synthetic_case().microgrids[0]))
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
        choices=["run", "cluster", "cluster-batch", "hierarchical", "group-scenarios", "field-audit", "network-scenarios", "compare-power-flow", "field-power-flow"],
    )
    parser.add_argument("--steps", type=int, default=96, help="24-hour time steps (96 = 15 min)")
    parser.add_argument("--output", type=Path, default=Path("results"))
    parser.add_argument("--input", type=Path, help="JSON request for cluster/compare-power-flow/field-power-flow, or request array for cluster-batch")
    parser.add_argument("--at", help="field-power-flow target ISO timestamp with timezone")
    parser.add_argument("--demo", action="store_true", help="explicitly allow synthetic field-power-flow demo inputs")
    parser.add_argument("--time-limit", type=float, default=180.0, help="seconds per solver call (cluster --input uses its solver options)")
    parser.add_argument(
        "--skip-fault-demo", action="store_true",
        help="skip the additional full rolling communication-fault study (hierarchical alias only)",
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
    elif args.command == "cluster-batch":
        if args.input is None:
            parser.error("cluster-batch requires --input containing a JSON request array")
        if not run_cluster_batch(args.input, args.output):
            raise SystemExit(1)
    elif args.command == "cluster":
        run_cluster(args.steps, args.output, args.time_limit, request_file=args.input)
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
        run(args.steps, args.output, args.time_limit)


if __name__ == "__main__":
    main()
