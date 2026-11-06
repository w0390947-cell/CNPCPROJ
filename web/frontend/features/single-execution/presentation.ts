import type {
  SingleExecution, UnifiedExecution, UnifiedStep, TubeState, UnifiedPlanCertificate, SingleEconomicProposal, TargetAdmission,
} from '../../shared/api/generated/single-execution';
import type { SimulationResult } from '../../lib/simulation-api';
import schema from '../../shared/api/generated/simulation-result.schema.json';
import { validate } from '../../shared/api/schema';

export type ExecutionPoint = {
  minute: number;
  state: TubeState;
  row?: UnifiedStep;
  p: number | null;
  q: number | null;
  targetP: number | null;
  targetQ: number | null;
  pError: number | null;
  qError: number | null;
};
export const value = (n: number | null | undefined, digits = 6) =>
  n == null || !Number.isFinite(n) ? '未记录' : n.toFixed(digits);
export const interval = (v?: { low: number; high: number } | null, digits = 6) =>
  v ? `[${value(v.low, digits)}, ${value(v.high, digits)}]` : '未记录';
export const clock = (minute: number) =>
  `${String(Math.floor(minute / 60)).padStart(2, '0')}:${String(minute % 60).padStart(2, '0')}`;
export const kindLabels = { wind: '风电', pv: '光伏', storage: 'PCS', svg: 'SVG' };
export type DisplayExecution = UnifiedExecution & {
  steps: UnifiedStep[]; plans: UnifiedPlanCertificate[];
  economic_proposals: SingleEconomicProposal[]; target_admissions: TargetAdmission[];
};
export function displayExecution(u: UnifiedExecution): DisplayExecution {
  return { ...u, steps: u.steps ?? [], plans: u.plans ?? [], economic_proposals: u.economic_proposals ?? [], target_admissions: u.target_admissions ?? [] };
}

export function executionPoints(u: UnifiedExecution): ExecutionPoint[] {
  return [{ minute: 0, state: u.initial, p: null, q: null, targetP: null, targetQ: null, pError: null, qError: null },
    ...(u.steps ?? []).map((row) => ({
      minute: row.step + 1,
      state: row.after,
      row,
      p: (row.pcc_p_mw.low + row.pcc_p_mw.high) / 2,
      q: (row.pcc_q_mvar.low + row.pcc_q_mvar.high) / 2,
      targetP: row.requested_p_mw,
      targetQ: row.requested_q_mvar,
      pError: Math.max(Math.abs(row.pcc_p_mw.low - row.requested_p_mw), Math.abs(row.pcc_p_mw.high - row.requested_p_mw)),
      qError: Math.max(Math.abs(row.pcc_q_mvar.low - row.requested_q_mvar), Math.abs(row.pcc_q_mvar.high - row.requested_q_mvar)),
    }))];
}

export function trackingPhase(u: UnifiedExecution, row?: UnifiedStep) {
  if (!row) return '初始状态';
  const t = u.plans?.[row.plan_index]?.tracking;
  if (!t) return '未设置跟踪承诺';
  if (row.step + 1 < row.deadline_step) return '响应过渡期';
  if (row.step + 1 > t.guaranteed_until_step) return '保证终点后记录';
  return row.tracking_certified ? '到期评价通过' : '到期评价未通过';
}

export function trackingStatistics(u: UnifiedExecution) {
  const rows = executionPoints(u).filter((p) => p.row && p.minute >= p.row.deadline_step);
  return {
    count: rows.length,
    passed: rows.filter((p) => p.row!.tracking_certified).length,
    maxP: rows.length ? Math.max(...rows.map((p) => p.pError!)) : null,
    maxQ: rows.length ? Math.max(...rows.map((p) => p.qError!)) : null,
  };
}

export function sumKind(u: UnifiedExecution, state: TubeState, kind: string, axis: 'p_mw' | 'q_mvar' = 'p_mw') {
  return u.domain.devices.resources.reduce((sum, r, i) => sum + (r.kind === kind ? state[axis][i] : 0), 0);
}

/** Shape and causal-axis validation for local viewing, never a new safety proof. */
export function assertExecutionAxes(e: SingleExecution) {
  if (!e.unified) return;
  const u = displayExecution(e.unified);
  const n = u.domain.devices.resources.length;
  const stateOK = (s: TubeState) => s.p_mw.length === n && s.q_mvar.length === n;
  const same = (a: TubeState, b: TubeState) => a.energy_mwh === b.energy_mwh &&
    a.p_mw.every((v, i) => v === b.p_mw[i]) && a.q_mvar.every((v, i) => v === b.q_mvar[i]);
  if (!n || !stateOK(u.initial) || e.completed_minutes !== u.steps.length || e.total_minutes !== u.requested_steps ||
      new Set(u.domain.devices.resources.map((r) => r.resource_id)).size !== n) throw new Error('执行时钟或资源轴不一致');
  const buses = u.domain.network.buses;
  if (!buses.length || u.domain.network.slack >= buses.length) throw new Error('网络根节点无效');
  const seen = new Set([u.domain.network.slack]);
  for (const b of u.domain.network.branches) {
    if (!seen.has(b.parent) || seen.has(b.child) || b.child >= buses.length) throw new Error('网络拓扑轴不一致');
    seen.add(b.child);
  }
  if (seen.size !== buses.length || u.domain.devices.resources.some((r) => !buses.includes(r.bus))) throw new Error('网络节点与资源接入不一致');
  let previous = u.initial;
  for (const [i, r] of u.steps.entries()) {
    if (r.step !== i || !stateOK(r.before) || !stateOK(r.after) || !same(previous, r.before) ||
        r.command.p_mw.length !== n || r.command.q_mvar.length !== n || !u.plans[r.plan_index] ||
        r.plan_offset !== i - u.plans[r.plan_index].start_step) throw new Error('执行记录存在状态、时钟或证书轴缺口');
    previous = r.after;
  }
  for (const p of u.economic_proposals) {
    if (!stateOK(p.initial) || !same(p.initial, u.steps[p.start_step]?.before ?? previous)) throw new Error('规划初态与已完成状态不一致');
    if (p.pq_witness) {
      const w = p.pq_witness;
      if (w.inputs.devices.resources.map((r) => r.resource_id).join('|') !== u.domain.devices.resources.map((r) => r.resource_id).join('|') ||
          w.commands.length !== w.inputs.planning_steps || w.endpoints.length !== w.inputs.planning_steps ||
          w.inputs.p_ceiling_mw.length !== w.inputs.planning_steps * w.inputs.planning_minutes + 1 ||
          w.inputs.p_ceiling_mw.some((v) => v.length !== n) ||
          w.commands.some((period) => period.length !== w.inputs.transition_minutes || period.some((c) => c.p_mw.length !== n || c.q_mvar.length !== n)) ||
          w.endpoints.some((c) => c.p_mw.length !== n || c.q_mvar.length !== n)) throw new Error('过渡见证资源轴不一致');
    }
  }
}

export function parseSingleResult(text: string): SimulationResult {
  const data: unknown = JSON.parse(text);
  validate(data, schema);
  const result = data as SimulationResult;
  if (result.metadata.scenario_type !== 'single_microgrid' || !result.single_execution) throw new Error('请选择单微网仿真结果 JSON');
  assertExecutionAxes(result.single_execution);
  return result;
}
