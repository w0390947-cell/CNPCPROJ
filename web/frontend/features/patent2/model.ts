import type { SimulationResult } from '@/lib/simulation-api';
import type { ClusterExecution } from '@/shared/api/generated/cluster-execution';

/** Partial committed evidence has no overall assessment or replay verdict. */
export type ExecutionView = Pick<
  ClusterExecution,
  | 'policy'
  | 'safety'
  | 'rolling_updates'
  | 'reserve_recovery_history'
  | 'reserve_observations'
> & {
  measurements: {
    region: string;
    time_minutes?: number[];
    p_mw?: number[];
    q_mvar?: number[];
    p_target_mw?: (number | null)[];
    q_target_mvar?: (number | null)[];
  }[];
};
export function executionView(e: ClusterExecution): ExecutionView {
  return {
    ...e,
    measurements: (
      e.stages.find((s) => s.stage === 'minute')?.regions ?? []
    ).map((r) => ({
      ...r,
      p_target_mw: r.p_target_samples_mw?.length
        ? r.p_target_samples_mw
        : r.p_target_mw,
      q_target_mvar: r.q_target_samples_mvar?.length
        ? r.q_target_samples_mvar
        : r.q_target_mvar,
    })),
  };
}

export const labels = { SC: 'SC', YA_B: 'YA_B', YA_C: 'YA_C' } as Record<
  string,
  string
>;
export const scenarioLabels = {
  normal: '正常初始电量',
  low_energy: '低电量恢复',
};
export const sourceLabels = {
  economic: '经济动作',
  backup: '认证后备',
  terminal: '终端控制',
  reserve: '备用调用',
};
export const statusLabels: Record<string, string> = {
  passed: '通过',
  violated: '未通过',
  unknown: '未获完整证明',
  not_computed: '未计算',
  certified: '通过',
  recovered: '已恢复',
  recovering: '恢复中',
  pending: '尚未到期',
  met: '按期完成',
  missed: '已逾期',
};
export const number = (v: number | null | undefined, digits = 4) =>
  v == null || !Number.isFinite(v) ? '—' : v.toFixed(digits);
export function reason(text?: string) {
  if (!text) return '未记录额外原因';
  return text
    .replaceAll(
      'joint continuous power factor unproven',
      '联合连续功率因数未获证明',
    )
    .replaceAll('joint power factor', '联合功率因数调用条件未获证明')
    .replaceAll('joint import capacity', '联合受电容量调用条件未获证明')
    .replaceAll('response command/power', '响应指令或功率边界')
    .replaceAll('one-minute ramp', '一分钟爬坡约束')
    .replaceAll('continuous AC box', '连续交流网络包络')
    .replaceAll('power factor envelope', '功率因数包络')
    .replaceAll(
      'full response/support energy',
      '完整响应及持续支撑的电量条件未获证明',
    )
    .replaceAll('standby energy not restored', '待命电量尚未恢复')
    .replaceAll('missing regional PCC enclosure', '缺少区域 PCC 调用包络')
    .replaceAll('voltage envelope', '电压包络');
}
/** History contains repeated observations of a task, not new tasks each minute. */
export function recoveryTasks(
  e: Pick<ClusterExecution, 'reserve_recovery_history'>,
) {
  const tasks = new Map<
    string,
    NonNullable<ClusterExecution['reserve_recovery_history']>[number]
  >();
  for (const task of e.reserve_recovery_history ?? []) {
    const key = `${task.region}:${task.direction}:${task.first_minute}:${task.deadline_minute}`;
    const previous = tasks.get(key);
    if (!previous || task.last_observed_minute >= previous.last_observed_minute)
      tasks.set(key, task);
  }
  return [...tasks.values()];
}
export function states(
  execution: Pick<ClusterExecution, 'safety'>,
  minute: number,
) {
  const safety = execution.safety;
  if (!safety) return [];
  if (minute === 0) return safety.domain.regions.map((r) => r.initial);
  return safety.steps?.find((s) => s.minute + 1 === minute)?.after ?? [];
}
/** Preparation may observe minute zero twice; show its latest recorded certificate. */
export function reserveNodes(
  e: Pick<ClusterExecution, 'reserve_observations'>,
) {
  const nodes = new Map<
    number,
    NonNullable<ClusterExecution['reserve_observations']>[number]
  >();
  for (const certificate of e.reserve_observations ?? [])
    nodes.set(certificate.start_minute, certificate);
  return [...nodes.values()].sort((a, b) => a.start_minute - b.start_minute);
}
export function metrics(result: SimulationResult) {
  const e = result.cluster_execution!;
  const steps = e.safety?.steps ?? [];
  return {
    completed: steps.length,
    economic: steps.filter((s) => s.source === 'economic').length,
    backup: steps.filter((s) => s.source === 'backup').length,
    adopted: (e.rolling_updates ?? []).filter((w) => w.adopted).length,
    replayed: result.patent2_evidence?.replayed_minutes,
    status: e.status,
  };
}
export function keyMinutes(
  e: Pick<
    ClusterExecution,
    'safety' | 'rolling_updates' | 'reserve_recovery_history'
  >,
) {
  const max = e.safety?.steps?.length ?? 0;
  return [
    ...new Set([
      0,
      max,
      ...(e.rolling_updates ?? []).map((w) => w.start_minute),
      ...(e.reserve_recovery_history ?? []).flatMap((t) => [
        t.first_minute,
        t.deadline_minute,
        t.completed_minute ?? -1,
      ]),
    ]),
  ]
    .filter((m) => m >= 0 && m <= max)
    .sort((a, b) => a - b);
}
