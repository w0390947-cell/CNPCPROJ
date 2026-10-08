import type { ClusterExecution } from '@/shared/api/generated/cluster-execution';
import type { ACRecoveryEvidence } from '@/shared/api/generated/cluster-execution';
import type {
  ComputationQualityReport,
  OptimizationQuality,
} from '@/shared/api/generated/computation-quality';

const statusLabels = {
  satisfied: '达到规定精度',
  budget_exhausted: '预算耗尽，尚未达到规定精度',
  infeasible: '求解器报告不可行',
  stopped: '停止，未确认达到规定精度',
};

export function ComputationQualityDetails({
  quality,
  execution,
  mode = 'cluster',
}: {
  quality: ComputationQualityReport;
  execution?: ClusterExecution | null;
  mode?: 'single' | 'cluster';
}) {
  const rows: Array<{ scope: string; records: OptimizationQuality[]; recovery?: ACRecoveryEvidence | null }> = [
    ...Object.entries(quality.reference_optimizations).map(
      ([scope, records]) => ({
        scope:
          scope === 'baseline'
            ? '无储能参考'
            : mode === 'single'
              ? '单微网优化'
              : '集中式参考',
        records,
        recovery: quality.reference_ac_recovery?.[scope],
      }),
    ),
    ...(execution?.stages
      .filter((s) => s.stage === 'day_ahead')
      .flatMap((s) =>
        (s.regions ?? []).map((r) => ({
          scope: `日前 ${r.region}`,
          records: r.optimization_quality ?? [],
          recovery: r.ac_recovery,
        })),
      ) ?? []),
    ...(execution?.rolling_updates?.flatMap((w) =>
      Object.entries(w.optimization_quality ?? {}).map(([region, records]) => ({
        scope: `第 ${w.start_minute} 分钟 ${region}`,
        records,
        recovery: w.ac_recovery?.[region],
      })),
    ) ?? []),
  ];
  const coordination = [
    { scope: '日前协调', records: quality.reference_coordination },
    ...(execution?.rolling_updates?.map((w) => ({
      scope: `第 ${w.start_minute} 分钟协调`,
      records: w.coordination_quality ?? [],
    })) ?? []),
  ];
  return (
    <details aria-label="求解质量与计算预算">
      <summary>求解质量与计算预算</summary>
      <p>
        质量优先：相对最优性差距要求 ≤{' '}
        {quality.policy.relative_gap == null
          ? '未记录'
          : `${(quality.policy.relative_gap * 100).toFixed(2)}%`}
        。 优化分级预算 {quality.policy.solve_seconds?.join(' / ') ?? '未记录'}{' '}
        秒
        {mode === 'cluster' && (
          <>
            ；协调分级上限{' '}
            {quality.policy.admm_iterations?.join(' / ') ?? '未记录'} 轮
          </>
        )}
        。
      </p>
      <p>
        仅预算不足时增加计算机会，优化重试使用独立预算。优化精度与独立校核分别记录。
        {mode === 'cluster' &&
          '协调收敛不代表整个系统已证明全局最优，ADMM 轮次上限为累计值。'}
      </p>
      <table>
        <thead>
          <tr>
            <th>计算范围</th>
            <th>最终质量</th>
            <th>各次求解记录</th>
          </tr>
        </thead>
        <tbody>
          {rows
            .filter((r) => r.records.length || r.recovery)
            .map(({ scope, records, recovery }) => (
              <tr key={scope}>
                <td>{scope}</td>
                <td>{recovery
                  ? recovery.outcome !== 'certified' ? 'AC 恢复未获认证'
                    : recovery.original_optimality === 'proven' ? 'AC 恢复通过，原目标达到规定精度'
                    : 'AC 可行恢复通过；原经济目标最优性未证明'
                  : statusLabels[records.at(-1)!.status]}</td>
                <td>
                  {recovery?.attempts?.map((attempt, index) => (
                    <div key={`ac-${index}`}>
                      {attempt.phase === 'physical_feasibility' ? '物理可行性恢复' : '原目标改进'}：
                      {attempt.certified ? '认证通过' : '未获认证'}；{attempt.solver_status}；
                      {attempt.elapsed_seconds.toPrecision(3)} 秒
                      {recovery.selected_attempt === index + 1 ? '（保留候选）' : ''}。
                      {attempt.phase === 'physical_feasibility' && '此阶段的求解间隙不代表经济最优性。'}
                    </div>
                  ))}
                  {records.map((record, index) => (
                    <div key={index}>
                      模型求解 {index + 1}：
                      {record.attempts
                        .map(
                          (a, i) =>
                            `${a.budget_seconds} 秒 / ${a.solver_status} / ${a.feasible ? '有可行解' : '无可用解'} / 差距 ${a.relative_gap == null ? '未记录' : `${(a.relative_gap * 100).toPrecision(3)}%`}${i + 1 === record.selected_attempt ? '（保留）' : ''}`,
                        )
                        .join('；')}
                    </div>
                  ))}
                </td>
              </tr>
            ))}
        </tbody>
      </table>
      {mode === 'cluster' && (
        <table>
          <thead>
            <tr>
              <th>协调范围</th>
              <th>各级预算与收敛证据</th>
            </tr>
          </thead>
          <tbody>
            {coordination
              .filter((r) => r.records.length)
              .map(({ scope, records }) => (
                <tr key={scope}>
                  <td>{scope}</td>
                  <td>
                    {records.map((r) => (
                      <div key={r.iteration_budget}>
                        上限 {r.iteration_budget} 轮，完成{' '}
                        {r.completed_iterations} 轮：
                        {r.converged ? '已收敛' : '未收敛'}； 一致性残差{' '}
                        {r.primal_residual.toPrecision(3)}（阈值{' '}
                        {r.primal_tolerance.toPrecision(3)}）， 更新残差{' '}
                        {r.dual_residual.toPrecision(3)}（阈值{' '}
                        {r.dual_tolerance.toPrecision(3)}）
                      </div>
                    ))}
                  </td>
                </tr>
              ))}
          </tbody>
        </table>
      )}
      <p>
        策略版本：{quality.policy.version}。未记录的历史结果不补造质量证据。
      </p>
    </details>
  );
}
