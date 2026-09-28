"""Causal ordering of synchronous cluster planning and continuous execution."""

from collections.abc import Callable

from .contracts import ExecutionPolicy, RollingProgress, RollingUpdate


def run_feedback_loop(
    *,
    total_minutes: int,
    policy: ExecutionPolicy,
    prepare: Callable[[int, int, int], RollingUpdate],
    advance: Callable[[int, int], dict[str, float]],
    progress: Callable[[str], None],
    rolling_progress: Callable[[RollingProgress], None] | None = None,
) -> tuple[RollingUpdate, ...]:
    """Never advance an unadopted plan; never replan before feedback is available."""
    updates: list[RollingUpdate] = []
    total_windows = (total_minutes + policy.update_minutes - 1) // policy.update_minutes
    for start in range(0, total_minutes, policy.update_minutes):
        end = min(total_minutes, start + policy.update_minutes)
        horizon_end = min(total_minutes, start + policy.horizon_minutes)
        progress(
            f"滚动协调 {start // 60:02d}:{start % 60:02d}：反馈电量、更新目标并校核设备计划"
        )
        observation = RollingProgress(
            current_window=len(updates) + 1,
            completed_windows=len(updates),
            total_windows=total_windows,
            start_minute=start,
            end_minute=end,
            total_minutes=total_minutes,
            phase="preparing",
        )
        if rolling_progress is not None:
            rolling_progress(observation)
        try:
            update = prepare(start, end, horizon_end)
        except (ValueError, RuntimeError, FloatingPointError) as exc:
            update = RollingUpdate(
                start_minute=start,
                end_minute=end,
                horizon_end_minute=horizon_end,
                status="unknown",
                reason=f"滚动准备失败，未执行后续计划：{exc}",
            )
        if (update.start_minute, update.end_minute, update.horizon_end_minute) != (
            start,
            end,
            horizon_end,
        ):
            raise ValueError("rolling evidence does not match the requested window")
        if not update.adopted:
            updates.append(update)
            if rolling_progress is not None:
                rolling_progress(observation.model_copy(update={"phase": "stopped"}))
            break
        if rolling_progress is not None:
            rolling_progress(observation.model_copy(update={"phase": "executing"}))
        try:
            energy = advance(start, end)
        except (ValueError, RuntimeError, FloatingPointError) as exc:
            updates.append(
                update.model_copy(
                    update={"status": "unknown", "reason": f"分钟执行中止：{exc}"}
                )
            )
            if rolling_progress is not None:
                rolling_progress(observation.model_copy(update={"phase": "stopped"}))
            break
        updates.append(update.model_copy(update={"actual_end_energy_mwh": energy}))
        if rolling_progress is not None:
            rolling_progress(
                observation.model_copy(
                    update={
                        "phase": "completed",
                        "completed_windows": len(updates),
                    }
                )
            )
    return tuple(updates)
