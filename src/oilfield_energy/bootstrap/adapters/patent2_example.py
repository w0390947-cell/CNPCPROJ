"""Use the existing study builder; never replace computed outcomes with document values."""
from hashlib import sha256

from oilfield_energy.modules.studies.contracts_patent2 import (
    Patent2Inputs, Patent2Region, Patent2Branch, Patent2Scenario, Patent2Evidence,
)
from oilfield_energy.data import ProjectCase
from oilfield_energy.network_model import validate_legacy_network_alignment
from oilfield_energy.workflows.cluster_execution.contracts import ClusterExecution
from oilfield_energy.workflows.cluster_execution.api import replay_cluster_safety
from .cluster_study import build_cluster_study, ClusterStudySpec
from .demo_case import encode
from .source_snapshot import source_digest as source_digest


def build_example(scenario: Patent2Scenario) -> ProjectCase:
    return build_cluster_study(ClusterStudySpec(low_energy=scenario == "low_energy"))


def describe_example(scenario: Patent2Scenario, case: ProjectCase | None = None) -> Patent2Inputs:
    case = case if case is not None else build_example(scenario)
    snapshot = encode(case)
    regions = []
    for m in case.microgrids:
        network = validate_legacy_network_alignment(m)
        regions.append(Patent2Region(
            name=m.name, buses=tuple(m.buses),
            branches=tuple(Patent2Branch(**{k: getattr(b, k) for k in
                ("name", "parent", "child", "r_pu", "x_pu", "s_max_mva", "tap_ratio")})
                for b in network.branches),
            initial_energy_mwh=m.storage.e_initial_mwh,
            minimum_energy_mwh=m.storage.e_min_mwh, maximum_energy_mwh=m.storage.e_max_mwh,
            storage_power_mw=m.storage.p_max_mw, eta_charge=m.storage.eta_charge,
            eta_discharge=m.storage.eta_discharge,
            **{k: {bus: tuple(map(float, v)) for bus, v in getattr(m, k).items()}
               for k in ("load_p_mw", "load_q_mvar", "wind_available_mw", "pv_available_mw")},
            initial_p_mw=m.initial_resource_p_mw, initial_q_mvar=m.initial_resource_q_mvar,
        ))
    return Patent2Inputs(scenario=scenario, regions=tuple(regions),
        price_cny_per_mwh=tuple(map(float, case.price_cny_per_mwh)),
        cluster_limit_mw=case.cluster_import_limit_mw,
        input_sha256=sha256(snapshot).hexdigest(), input_snapshot_json=snapshot.decode("utf-8"))


def finish_example(inputs: Patent2Inputs, execution: ClusterExecution | None, *, code_sha256: str | None = None) -> Patent2Evidence:
    replayed = None
    error = ""
    try:
        if execution is None or execution.safety is None or execution.safety.initial_continuation is None:
            error = "缺少初始认证或执行证据，未进行回放验证"
        else:
            # Verify the serialized evidence, not just the in-memory object.
            safety = type(execution.safety).model_validate_json(execution.safety.model_dump_json())
            replayed = replay_cluster_safety(safety)
    except (ValueError, RuntimeError, ArithmeticError) as exc:
        error = str(exc)
    return Patent2Evidence(inputs=inputs, replayed_minutes=replayed,
        replay_error=error, code_sha256=code_sha256 or source_digest(),
        code_snapshot_phase='before_run' if code_sha256 else 'after_run')
