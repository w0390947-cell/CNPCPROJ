"""Progress survives persistence; computation completion cannot publish success."""

from pathlib import Path

import pytest

from oilfield_energy import job_worker
from oilfield_energy.job_manager import JobStatus, progress_status, write_job_status
from oilfield_energy.service import ProgressUpdate, SimulationRequest, SimulationResult
from oilfield_energy.workflows.cluster_execution.contracts import RollingProgress


def initial():
    return JobStatus(
        simulation_id="a" * 32,
        name="progress",
        state="running",
        created_at="2026-09-25T00:00:00+00:00",
        updated_at="2026-09-25T00:00:00+00:00",
    )


def observation():
    return RollingProgress(
        current_window=2,
        completed_windows=1,
        total_windows=3,
        start_minute=15,
        end_minute=30,
        total_minutes=35,
        phase="executing",
    )


def test_restored_progress_and_old_status_preserve_their_evidence():
    status = progress_status(
        initial(),
        ProgressUpdate(
            stage="validating",
            label="executing",
            sequence=4,
            rolling=observation(),
        ),
    )
    restored = JobStatus.model_validate_json(status.model_dump_json())
    assert restored.rolling == observation()
    later = progress_status(
        restored, ProgressUpdate(stage="serializing", label="saving", sequence=5)
    )
    assert later.rolling == restored.rolling
    assert initial().rolling is None
    old = initial().model_dump(exclude={"rolling"})
    assert JobStatus.model_validate(old).rolling is None


@pytest.mark.parametrize("state", ["failed", "cancelled", "interrupted", "succeeded"])
def test_late_progress_cannot_revive_a_terminal_job(state):
    status = initial().model_copy(update={"state": state, "rolling": observation()})
    assert (
        progress_status(
            status,
            ProgressUpdate(
                stage="validating",
                label="late",
                sequence=4,
            ),
        )
        == status
    )


@pytest.mark.parametrize("save_fails", [False, True])
def test_worker_stays_in_saving_state_until_result_is_readable(
    tmp_path, monkeypatch, save_fails
):
    fixture = (
        Path(__file__).resolve().parents[3]
        / "web/frontend/tests/fixtures/synthetic-single-result.json"
    )
    result = SimulationResult.model_validate_json(fixture.read_bytes())
    # A failed engineering verdict is still a completed computation.
    result = result.model_copy(
        update={
            "executive_summary": result.executive_summary.model_copy(
                update={"overall_passed": False},
            )
        }
    )
    write_job_status(tmp_path, initial())
    (tmp_path / "request.json").write_text(
        SimulationRequest().model_dump_json(), encoding="utf-8"
    )

    def compute(_request, callback):
        callback(
            ProgressUpdate(
                stage="validating", label="executing", sequence=4, rolling=observation()
            )
        )
        callback(ProgressUpdate(stage="succeeded", label="计算完成", sequence=6))
        return result

    original_write = job_worker.atomic_write_json

    def write_result(path, payload):
        during = JobStatus.model_validate_json((tmp_path / "status.json").read_bytes())
        assert during.state.value == "running"
        assert during.stage == "serializing"
        assert during.stage_label == "正在保存仿真结果"
        assert not during.result_available
        assert not path.exists()
        if save_fails:
            raise OSError("injected disk failure")
        original_write(path, payload)

    monkeypatch.setattr(job_worker, "run_simulation", compute)
    monkeypatch.setattr(job_worker, "atomic_write_json", write_result)
    if save_fails:
        with pytest.raises(OSError, match="disk failure"):
            job_worker.run_session(tmp_path)
    else:
        job_worker.run_session(tmp_path)
        saved = SimulationResult.model_validate_json(
            (tmp_path / "result.json").read_bytes()
        )
        assert not saved.executive_summary.overall_passed
    terminal = JobStatus.model_validate_json((tmp_path / "status.json").read_bytes())
    assert terminal.state.value == ("failed" if save_fails else "succeeded")
    assert terminal.result_available is not save_fails
    assert terminal.rolling == observation()
