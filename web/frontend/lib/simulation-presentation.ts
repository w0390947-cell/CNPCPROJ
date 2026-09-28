import type { SimulationConfiguration, SimulationResult } from './simulation-api';

export function numberLabel(value: number | null | undefined, digits = 2): string {
  return value == null || !Number.isFinite(value) ? '—' : value.toFixed(digits);
}

export function clockLabel(minute: number): string {
  const rounded = Math.max(0, Math.round(minute));
  return `${String(Math.floor(rounded / 60)).padStart(2, '0')}:${String(rounded % 60).padStart(2, '0')}`;
}

export type ProfileFrame = {
  time: string; minute: number;
  import: number | null; load: number | null; renewable: number | null;
  wind: number | null; pv: number | null; storage: number | null; svg: number | null;
  windQ: number | null;
};
export const emptyFrame: ProfileFrame = {
  time: '—', minute: 0, import: null, load: null, renewable: null,
  wind: null, windQ: null, pv: null, storage: null, svg: null,
};

/** 原始计划点：不补造任何曲线，也不把风光总和当作光伏。 */
export function profileFrames(result: SimulationResult | null, single = false): ProfileFrame[] {
  if (!result) return [];
  if (!single && result.cluster_timeseries.length) return result.cluster_timeseries.map(p => ({
    ...emptyFrame, time: clockLabel(p.time_hour * 60), minute: p.time_hour * 60,
    import: p.aggregate_import_mw, load: p.aggregate_load_mw, renewable: p.aggregate_renewable_mw,
  }));
  return result.timeseries.map(p => ({
    time: clockLabel(p.time_hour * 60), minute: p.time_hour * 60,
    import: p.p_grid_optimized_mw, load: p.load_mw, renewable: p.wind_used_mw + p.pv_used_mw,
    wind: p.wind_used_mw, pv: p.pv_used_mw,
    windQ: p.wind_q_mvar ?? null,
    storage: p.storage_discharge_mw - p.storage_charge_mw, svg: p.svg_q_mvar,
  }));
}

/** 只在已有采样点之间插值；绝不把末点绕回次日首点，不外推缺失的尾段。 */
export function minuteFrames(points: ProfileFrame[]): ProfileFrame[] {
  if (points.length < 2) return points;
  const frames: ProfileFrame[] = [];
  for (let i = 0; i < points.length - 1; i++) {
    const left = points[i], right = points[i + 1];
    if (right.minute <= left.minute) throw new Error('计划时间轴必须严格递增');
    for (let minute = left.minute; minute < right.minute; minute++) {
      const ratio = (minute - left.minute) / (right.minute - left.minute);
      const frame = { ...emptyFrame, minute, time: clockLabel(minute) };
      for (const key of ['import', 'load', 'renewable', 'wind', 'windQ', 'pv', 'storage', 'svg'] as const) {
        const a = left[key], b = right[key];
        frame[key] = a == null || b == null ? null : a + (b - a) * ratio;
      }
      frames.push(frame);
    }
  }
  frames.push(points[points.length - 1]);
  return frames;
}

export const groupScenarioLabels: Record<string, string> = {
  normal_operation: '正常运行', emergency_curtailment: '紧急限发',
  stable_three_consecutive: '连续三次越限', stable_three_of_five: '五周期三次越限',
  consecutive_three_trigger: '连续三次越限', three_in_five_debounce: '五周期三次越限',
  recovery_response_failure: '恢复响应偏差', communication_failure_and_reentry: '通信故障与恢复复归',
  successful_progressive_recovery: '渐进恢复成功', recovery_abort: '恢复中止',
  insufficient_pv_capacity: '光伏调节能力不足', secondary_curtailment: '二次限发',
};
export const stateLabels: Record<string, string> = {
  normal: '正常运行', prepared: '调控准备', risk_observing: '风险观察', curtailing: '执行限发',
  curtailed_hold: '限发保持', restore_wait: '恢复等待', restoring: '恢复监测',
  restoring_dwell: '恢复监测', recovery_aborted: '恢复中止', recovery_inhibit: '恢复闭锁',
  output_block: '停止新写入', hard_override: '硬保护接管',
};
export const actionLabels: Record<string, string> = {
  none: '保持监测', curtail: '下发限发', restore: '增量恢复',
  abort_restore: '中止恢复', abort_recovery: '中止恢复', hold: '保持限发', block: '停止新写入',
};

export function outageAtIteration(result: SimulationResult | null, iteration: number, region?: string): boolean {
  const c = result?.communication;
  const outages = c?.event_executions?.filter(event => event.window.kind === 'outage');
  if (outages?.length) return outages.some(event =>
    (region == null || event.window.target === region) && event.observed_ticks.includes(iteration));
  return c?.outage_start_iteration != null && c.outage_end_iteration != null
    && (region == null || c.outage_region === region)
    && iteration >= c.outage_start_iteration && iteration <= c.outage_end_iteration;
}

/** 事件分支沿用基线请求，不能混入尚未重新计算的界面草稿参数。 */
export function resultConfiguration(result: SimulationResult): SimulationConfiguration {
  const r = result.request;
  return {
    region: r.region, steps: r.steps, storageEnabled: r.storage_enabled,
    timeLimitSeconds: r.solver.time_limit_seconds, admmMaxIterations: r.admm_max_iterations,
    communicationLossProbability: r.communication_loss_probability,
    communicationMaxDelayIterations: r.communication_max_delay_iterations,
    communicationOutageRegion: r.communication_outage_region,
    communicationOutageStartIteration: r.communication_outage_start_iteration,
    communicationOutageEndIteration: r.communication_outage_end_iteration,
  };
}
