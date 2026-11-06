"""Describe the fixed example using the production model preparation path."""
from functools import lru_cache
from .project_dataset import build_synthetic_case
from .single_unified import prepare_unified_single, describe_single_forecast
from oilfield_energy.workflows.single_execution.contracts_example import SingleExampleInputs


@lru_cache(maxsize=1)
def describe_single_example() -> SingleExampleInputs:
    from .single_planning import single_execution_policy
    case = build_synthetic_case(steps=8)
    policy = single_execution_policy(None)
    domain, initial, intraday, _, basis, total = prepare_unified_single(
        case, "SC", storage_enabled=True, packaged_inputs=True, events=[], policy=policy,
    )
    return SingleExampleInputs(
        region="SC", total=total, update_steps=policy.update_minutes,
        reference_time_hours=tuple(float(v) for v in case.time_hours),
        dataset_sha256=case.dataset_sha256 or "", domain=domain, initial=initial,
        dataset_id=case.dataset_id, dataset_revision=case.dataset_revision,
        execution_version=policy.single_execution_version, input_basis=basis,
        input_forecast=describe_single_forecast(intraday, policy.update_minutes),
    )
