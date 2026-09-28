import type { JobStatus } from '../../shared/api/generated/job-status';

const minuteLabel = (minute: number) =>
  `${Math.floor(minute / 60)
    .toString()
    .padStart(2, '0')}:${(minute % 60).toString().padStart(2, '0')}`;

/** Stage/work fraction only: never an elapsed-time fraction or a safety verdict. */
export function jobProgressPresentation(
  status: JobStatus | null,
  observing = true,
) {
  const rolling = status?.rolling;
  const active = observing && status?.state === 'running';
  const totalStages = status?.total_stages ?? 6;
  const sequence = status?.stage_sequence ?? 1;
  const withinStage =
    sequence === 4 && rolling
      ? rolling.completed_windows / rolling.total_windows
      : 0;
  const finished = status?.state === 'succeeded' && status.result_available;
  // A stage notification means the stage started; only published results reach 100.
  const fraction =
    status?.state === 'queued' ? 0 : (sequence - 1 + withinStage) / totalStages;
  const percent = finished
    ? 100
    : Math.min(99.9, Math.max(0, Math.round(fraction * 1000) / 10));
  const stateLabel =
    status?.state === 'succeeded'
      ? finished
        ? '任务已完成'
        : '结果待确认'
      : status?.state === 'failed'
        ? '任务失败'
        : status?.state === 'cancelled'
          ? '任务已取消'
          : status?.state === 'interrupted'
            ? '任务已中断'
            : status?.state === 'queued'
              ? '排队中'
              : active
                ? '运行中'
                : '状态待确认';
  let detail: string | null = null;
  if (rolling) {
    const current =
      active && status?.stage === 'validating' && rolling.phase !== 'stopped';
    const action =
      rolling.phase === 'preparing'
        ? '协调与设备计划'
        : rolling.phase === 'executing'
          ? '设备执行与反馈'
          : rolling.phase === 'completed'
            ? '执行与反馈已完成'
            : '滚动计算已中止';
    detail = `${current ? '当前' : '最后记录'}窗口 ${rolling.current_window}/${rolling.total_windows} · 仿真 ${minuteLabel(rolling.start_minute)}–${minuteLabel(rolling.end_minute)} · 已完成 ${rolling.completed_windows}/${rolling.total_windows} 个窗口${current || rolling.phase === 'stopped' ? ` · ${action}` : ''}`;
  }
  return { percent, stateLabel, detail };
}
