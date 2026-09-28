import type { ClusterExecution } from '@/shared/api/generated/cluster-execution';
import type { ComputationQualityReport } from '@/shared/api/generated/computation-quality';
import { ComputationQualityDetails } from '@/features/computation-quality';
import { DynamicTrackingDetails } from './dynamic-tracking';
import { planTimeLabel } from './presentation';
import { ReserveDetails } from './reserve-details';
import styles from './style.module.css';

export function ClusterExecutionPanel({
  quality,
  execution,
  hasResult,
  busy,
}: {
  quality?: ComputationQualityReport | null;
  execution?: ClusterExecution | null;
  hasResult: boolean;
  busy: boolean;
}) {
  return (
    <section className={`panel ${styles.panel}`} aria-label="集群执行校核">
      <div className="panel-title">
        <h3>执行校核</h3>
        <em>
          {busy
            ? hasResult
              ? '计算中 · 当前显示上次结果'
              : '正在计算'
            : '模拟执行证据'}
        </em>
      </div>
      {quality && (
        <ComputationQualityDetails quality={quality} execution={execution} />
      )}
      {execution ? (
        <ExecutionEvidence execution={execution} />
      ) : (
        <p className={styles.emptyExecution}>
          {hasResult
            ? '本次结果未记录执行校核证据，不能确认执行是否通过。'
            : '运行仿真后显示执行校核结果。'}
        </p>
      )}
    </section>
  );
}

export function ExecutionEvidence({
  execution,
}: {
  execution: ClusterExecution;
}) {
  return (
    <div className={styles.execution} aria-label="自动执行校核">
      <strong>自动执行校核 · 模拟运行</strong>
      <ol>
        {execution.stages.map((stage) => (
          <li key={stage.stage} data-status={stage.status}>
            <span>
              {
                {
                  day_ahead: '日前计划落实',
                  intraday: '日内更新',
                  minute: '分钟级响应',
                }[stage.stage]
              }
            </span>
            <b>
              {
                {
                  passed: '通过',
                  violated: '未通过',
                  unknown: '无法确认',
                  not_computed: '未执行',
                }[stage.status]
              }
            </b>
          </li>
        ))}
      </ol>
      <p>
        一次仿真依次执行以上阶段；中止或缺少证据时保留实际状态。具体偏差与约束见运行结论。
      </p>
      {!!execution.rolling_updates?.length && (
        <>
          <p>
            自动滚动更新：已采用{' '}
            {execution.rolling_updates.filter((row) => row.adopted).length}{' '}
            个窗口 · 每 {execution.policy.update_minutes ?? '未记录'} 分钟更新 ·
            向前规划{' '}
            {execution.policy.horizon_minutes == null
              ? '未记录'
              : execution.policy.horizon_minutes / 60}{' '}
            小时
          </p>
          {execution.rolling_updates
            .filter((row) => row.reason)
            .map((row) => (
              <p key={row.start_minute}>
                {Math.floor(row.start_minute / 60)
                  .toString()
                  .padStart(2, '0')}
                :{(row.start_minute % 60).toString().padStart(2, '0')} ·{' '}
                {row.reason}
              </p>
            ))}
        </>
      )}
      <p>{execution.policy.source}</p>
      {execution.policy.reactive_planning && (
        <details className={styles.details}>
          <summary>无功计划余量与动态跟踪</summary>
          <p>
            日内计划预留{' '}
            {100 * (execution.policy.reactive_planning.reserve_fraction ?? 0)}%
            的无功能力，并依据已执行设备状态约束计划过渡。分钟控制在设备权限和
            AC 安全条件内修正实际无功偏差；响应期限及跟踪限值仍独立校核。
          </p>
          <p>{execution.policy.reactive_planning.source}</p>
        </details>
      )}
      <p>{execution.input_basis}</p>
      {execution.stages
        .filter((stage) => stage.reason)
        .map((stage) => (
          <p key={stage.stage}>{stage.reason}</p>
        ))}
      <details className={styles.details}>
        <summary>
          滚动窗口记录（{execution.rolling_updates?.length ?? 0} 条）
        </summary>
        {execution.rolling_updates?.length ? (
          <div className={styles.windowScroll}>
            <table className={styles.windowTable}>
              <caption>窗口采用与执行反馈；未记录反馈不视为已完成</caption>
              <thead>
                <tr>
                  <th>仿真时段</th>
                  <th>规划截至</th>
                  <th>窗口校核</th>
                  <th>计划采用</th>
                  <th>执行反馈</th>
                  <th>ADMM 迭代</th>
                  <th>说明</th>
                </tr>
              </thead>
              <tbody>
                {execution.rolling_updates.map((row) => (
                  <tr key={row.start_minute}>
                    <td>
                      {planTimeLabel(row.start_minute / 60)}—
                      {planTimeLabel(row.end_minute / 60)}
                    </td>
                    <td>{planTimeLabel(row.horizon_end_minute / 60)}</td>
                    <td>
                      {
                        {
                          passed: '通过',
                          violated: '未通过',
                          unknown: '无法确认',
                          not_computed: '未执行',
                        }[row.status]
                      }
                    </td>
                    <td>
                      {row.adopted === true
                        ? '已采用'
                        : row.adopted === false
                          ? '未采用'
                          : '未记录'}
                    </td>
                    <td>
                      {Object.keys(row.actual_end_energy_mwh ?? {}).length
                        ? '已记录'
                        : '未记录'}
                    </td>
                    <td>{row.admm_iterations ?? '未记录'}</td>
                    <td>{row.reason || '—'}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        ) : (
          <p>本次结果没有滚动窗口记录，不能据此推定全天执行完成。</p>
        )}
      </details>
      <DynamicTrackingDetails
        regions={
          execution.stages.find((stage) => stage.stage === 'minute')?.regions ??
          []
        }
        policy={execution.policy.dynamic_tracking}
      />
      <ReserveDetails execution={execution} />
    </div>
  );
}
