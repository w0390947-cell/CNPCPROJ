"""Entry adapters preserve requests, decisions, rejected evidence and errors."""

import json
from dataclasses import replace

import numpy as np
import pytest

from oilfield_energy import cli, job_worker, service, web_api
from oilfield_energy.bootstrap.adapters import cluster_study
from oilfield_energy.bootstrap.adapters.cluster_execution import ExecutionEngines
from oilfield_energy.bootstrap.adapters.project_dataset import build_synthetic_case
from oilfield_energy.workflows.cluster_execution.contracts import ClusterExecution


@pytest.fixture(scope="module")
def rejected_result():
    case = build_synthetic_case(4)
    case = replace(
        case,
        time_hours=np.arange(4) / 4,
        assumptions=replace(case.assumptions, dt_hours=0.25),
    )
    # A genuine nonconvergence result, not a hand-built passing certificate.
    result = service.run_simulation(
        service.SimulationRequest(
            scenario_type="cluster_coordination", steps=4, admm_max_iterations=4
        ),
        input_case=case,
    )
    assert result.cluster_execution.status != "passed"
    return result


def test_web_worker_and_file_adapter_share_the_service():
    assert web_api.run_simulation is service.run_simulation
    assert job_worker.run_simulation is service.run_simulation
    assert cluster_study.service is service


def test_cli_and_batch_preserve_request_and_unmodified_rejection(
    monkeypatch, tmp_path, rejected_result
):
    requests = [
        service.SimulationRequest(
            scenario_type="cluster_coordination", steps=4, storage_enabled=False
        ),
        service.SimulationRequest(
            scenario_type="communication_fault", steps=8, admm_max_iterations=81
        ),
    ]
    captured = []

    def observe(request, progress_callback=None, *, input_case=None):
        captured.append((request, input_case))
        return rejected_result

    monkeypatch.setattr(service, "run_simulation", observe)
    one = tmp_path / "one.json"
    one.write_text(requests[0].model_dump_json(), encoding="utf-8")
    with pytest.raises(RuntimeError, match="not certified"):
        cli.run_cluster(96, tmp_path / "cli", 1, request_file=one)
    batch = tmp_path / "requests.json"
    batch.write_text(
        json.dumps([r.model_dump(mode="json") for r in requests]), encoding="utf-8"
    )
    assert not cluster_study.run_cluster_batch(batch, tmp_path / "batch")
    assert [r for r, _ in captured] == [requests[0], *requests]
    assert all(case is None for _, case in captured)
    expected = rejected_result.model_dump(mode="json")
    for directory in (
        tmp_path / "cli",
        tmp_path / "batch/0000",
        tmp_path / "batch/0001",
    ):
        assert (
            json.loads((directory / "result.json").read_text(encoding="utf-8"))
            == expected
        )
        execution = ClusterExecution.model_validate_json(
            (directory / "cluster_execution.json").read_bytes()
        )
        assert (
            execution.status != "passed"
            and execution.algorithm_version == "cluster-rolling-v6"
        )


def test_batch_continues_after_error_and_preserves_each_diagnostic(
    monkeypatch, tmp_path, rejected_result
):
    calls = 0

    def fail_then_return(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("solver unavailable")
        return rejected_result

    monkeypatch.setattr(service, "run_simulation", fail_then_return)
    request = service.SimulationRequest(scenario_type="cluster_coordination", steps=4)
    path = tmp_path / "batch-input.json"
    path.write_text(json.dumps([request.model_dump(mode="json")] * 2), encoding="utf-8")
    output = tmp_path / "batch"
    assert not cluster_study.run_cluster_batch(path, output)
    assert calls == 2
    assert (
        json.loads((output / "0000/failure.json").read_text())["message"]
        == "solver unavailable"
    )
    assert (output / "0001/result.json").is_file()
    assert json.loads((output / "batch.json").read_text())[0]["status"] == "error"
    with pytest.raises(FileExistsError):
        cluster_study.run_cluster_study(request, output / "0001")
    assert calls == 2


def test_history_cannot_acquire_current_algorithm_identity(rejected_result):
    raw = rejected_result.cluster_execution.model_dump()
    raw.pop("algorithm_version")
    assert ClusterExecution.model_validate(raw).algorithm_version is None


def test_no_rolling_session_means_no_minute_certificate():
    engine = object.__new__(ExecutionEngines)
    engine.rolling = None
    evidence = engine.minute("SC")
    assert evidence.status == "not_computed" and not evidence.p_mw


def test_wrong_scenario_cannot_create_cluster_output(tmp_path):
    with pytest.raises(ValueError, match="cluster studies"):
        cluster_study.run_cluster_study(service.SimulationRequest(), tmp_path / "wrong")
    assert not (tmp_path / "wrong").exists()
