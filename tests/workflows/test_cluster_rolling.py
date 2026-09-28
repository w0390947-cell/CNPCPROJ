"""Causal ordering and failure boundaries, independent of numerical solvers."""

import pytest

from oilfield_energy.workflows.cluster_execution.api import run_feedback_loop
from oilfield_energy.workflows.cluster_execution.contracts import (
    ExecutionPolicy,
    RollingUpdate,
)


def test_progress_counts_only_feedback_and_uses_actual_grid_without_changing_results():
    def run(observe):
        energy = {"A": 2.0}

        def prepare(start, end, horizon):
            if observe:
                assert observations[-1].phase == "preparing"
                assert observations[-1].completed_windows == start // 7
            return RollingUpdate(
                start_minute=start,
                end_minute=end,
                horizon_end_minute=horizon,
                status="passed",
                adopted=True,
                initial_energy_mwh=dict(energy),
            )

        def advance(start, end):
            if observe:
                assert observations[-1].phase == "executing"
                assert observations[-1].completed_windows == start // 7
            energy["A"] -= (end - start) * 0.1
            return dict(energy)

        return run_feedback_loop(
            total_minutes=20,
            policy=ExecutionPolicy(update_minutes=7, horizon_minutes=14),
            prepare=prepare,
            advance=advance,
            progress=lambda _: None,
            rolling_progress=observations.append if observe else None,
        )

    observations = []
    assert run(True) == run(False)
    assert [p.phase for p in observations] == [
        "preparing",
        "executing",
        "completed",
    ] * 3
    assert [p.completed_windows for p in observations] == [0, 0, 1, 1, 1, 2, 2, 2, 3]
    assert {p.total_windows for p in observations} == {3}
    assert observations[-1].end_minute == observations[-1].total_minutes == 20
    assert observations[-1].start_minute == 14


@pytest.mark.parametrize("failure", ["prepare", "rejected", "advance"])
def test_stopped_progress_preserves_completed_prefix(failure):
    observations = []

    def prepare(start, end, horizon):
        if start and failure == "prepare":
            raise RuntimeError("solver failed")
        return RollingUpdate(
            start_minute=start,
            end_minute=end,
            horizon_end_minute=horizon,
            status="passed",
            adopted=not (start and failure == "rejected"),
        )

    def advance(start, _end):
        if start and failure == "advance":
            raise RuntimeError("execution failed")
        return {"A": 1.0}

    rows = run_feedback_loop(
        total_minutes=35,
        policy=ExecutionPolicy(),
        prepare=prepare,
        advance=advance,
        progress=lambda _: None,
        rolling_progress=observations.append,
    )
    assert len(rows) == 2
    assert observations[-1].phase == "stopped"
    assert observations[-1].completed_windows == 1
    assert observations[-1].current_window == 2
    assert observations[-1].total_windows == 3


def test_feedback_arrives_before_the_next_window_is_prepared():
    events = []
    energy = {"A": 2.0, "B": 3.0}

    def prepare(start, end, horizon):
        events.append(("plan", start, dict(energy)))
        return RollingUpdate(
            start_minute=start,
            end_minute=end,
            horizon_end_minute=horizon,
            status="passed",
            adopted=True,
            initial_energy_mwh=dict(energy),
        )

    def advance(start, end):
        events.append(("execute", start, end))
        energy["A"] -= 0.2
        energy["B"] += 0.1
        return dict(energy)

    rows = run_feedback_loop(
        total_minutes=35,
        policy=ExecutionPolicy(),
        prepare=prepare,
        advance=advance,
        progress=lambda _: None,
    )
    assert [e[0] for e in events] == ["plan", "execute"] * 3
    assert rows[1].initial_energy_mwh == rows[0].actual_end_energy_mwh
    assert rows[-1].end_minute == 35
    assert rows[0].initial_energy_mwh == {"A": 2.0, "B": 3.0}


@pytest.mark.parametrize("raises", [False, True])
def test_failed_update_never_executes_stale_targets(raises):
    executed = []

    def prepare(start, end, horizon):
        if start and raises:
            raise RuntimeError("solver timeout")
        return RollingUpdate(
            start_minute=start,
            end_minute=end,
            horizon_end_minute=horizon,
            status="passed" if start == 0 else "unknown",
            adopted=start == 0,
        )

    rows = run_feedback_loop(
        total_minutes=60,
        policy=ExecutionPolicy(),
        prepare=prepare,
        advance=lambda a, b: executed.append((a, b)) or {"A": 1.5},
        progress=lambda _: None,
    )
    assert executed == [(0, 15)]
    assert len(rows) == 2 and not rows[-1].adopted
    assert rows[0].actual_end_energy_mwh == {"A": 1.5}
