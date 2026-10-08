import type { ClusterExecution } from '@/shared/api/generated/cluster-execution';
import { planTimeLabel } from './presentation';
import styles from './style.module.css';

export function ReserveDetails({ execution }: { execution: ClusterExecution }) {
  const policy = execution.policy.storage_reserve;
  if (!policy || !execution.storage_enabled) return null;
  const observations = execution.minute_reserves ?? [];
  const changes = execution.reserve_target_adjustments ?? [];
  const delivery = execution.policy.reserve_delivery;
  const minutes = execution.safety
    ? new Set((execution.reserve_observations ?? []).map((row) => row.start_minute)).size
    : new Set(observations.map((row) => row.minute)).size;
  return (
    <details className={styles.details}>
      <summary>储能备用与分钟反馈（已检查 {minutes} 个分钟时刻）</summary>
      <p>
        日内计划预留双向调节功率和电量：预测负荷与风光可用量之和的{' '}
        {(policy.forecast_error_fraction ?? 0) * 100}% 加{' '}
        {policy.minimum_error_mw} MW， 覆盖 {policy.support_minutes}{' '}
        分钟。{delivery
          ? `首次未能确认完整备用后，须在 ${policy.recovery_minutes} 分钟内恢复；截止时间不随重规划重置。`
          : `备用消耗后按 ${policy.recovery_minutes} 分钟恢复要求重算。`}
      </p>
      <p>{policy.source}</p>
      {execution.safety ? <p>
        逐分钟使用实际 P/Q/E 重新观察完整备用；原恢复期限不重置。新一轮调用须在完整恢复、返回认证待命状态并重新认证后接纳。已接纳调用 {execution.safety.reserve_events?.length ?? 0} 轮。
      </p> : <p>
        低于所需备用的 {(policy.trigger_fraction ?? 0) * 100}%
        {delivery ? '为简化观察的紧急阈值；完整备用未获证明也会触发反馈。重规划通常至少间隔 ' : '时触发后续短窗口联合重规划，两次触发至少间隔 '}
        {policy.replan_cooldown_minutes} 分钟。
        {delivery
          ? `到期检查不受冷却限制。响应时限 ${delivery.response_minutes} 分钟；可交付性按记录的设备与网络模型检查连续区间。`
          : '分钟检查记录电量耐受和有功余量；设备动态、保护、潮流及跟踪偏差仍独立校核。'}
      </p>}
      <p>
        简化观察触发记录 {observations.filter((row) => row.deficient).length}{' '}
        条（按区域计）； 触发重规划 {changes.length} 次，其中采用{' '}
        {changes.filter((row) => row.update.adopted).length} 次。
        调整只影响后续分钟，原目标与已发生偏差保留在结果中。
      </p>
      {delivery && <p>
        完整备用观察：通过 {(execution.reserve_observations ?? []).filter((row) => row.status === 'certified').length} 条，
        未获证明 {(execution.reserve_observations ?? []).filter((row) => row.status === 'unknown').length} 条。
        未获证明不等于物理无解；恢复截止时刻和各轮证据保存在结果中。
      </p>}
      {!!changes.length && (
        <div className={styles.windowScroll}>
          <table className={styles.windowTable}>
            <caption>分钟反馈目标调整（正值为受电；展示调整首分钟）</caption>
            <thead>
              <tr>
                <th>时刻</th>
                <th>区域</th>
                <th>原 P / Q</th>
                <th>新 P / Q</th>
                <th>采用情况</th>
              </tr>
            </thead>
            <tbody>
              {changes.flatMap((row) =>
                Object.entries(row.original_p_mw).map(([region, p]) => (
                  <tr key={`${row.minute}-${region}`}>
                    <td>{planTimeLabel(row.minute / 60)}</td>
                    <td>
                      {region}
                      {row.trigger_regions.includes(region)
                        ? (delivery ? '（触发反馈）' : '（备用不足）')
                        : ''}
                    </td>
                    <td>
                      {p[0]?.toFixed(4)} MW /{' '}
                      {row.original_q_mvar[region]?.[0]?.toFixed(4)} Mvar
                    </td>
                    <td>
                      {row.update.adopted
                        ? `${row.adopted_p_mw?.[region]?.[0]?.toFixed(4)} MW / ${row.adopted_q_mvar?.[region]?.[0]?.toFixed(4)} Mvar`
                        : '未采用新目标'}
                    </td>
                    <td>
                      {row.update.adopted
                        ? '已采用'
                        : row.update.reason || '未通过采用校核'}
                    </td>
                  </tr>
                )),
              )}
            </tbody>
          </table>
        </div>
      )}
    </details>
  );
}
