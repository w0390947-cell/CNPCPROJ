import type {
  DynamicTrackingPolicy,
  RegionalExecution,
} from '@/shared/api/generated/cluster-execution';
import styles from './dynamic-tracking.module.css';

const statusLabel = { passed: '通过', violated: '未通过', unknown: '无法确认' };
const value = (number: number | null | undefined, unit = '') =>
  number == null
    ? '无足够证据'
    : `${number.toFixed(3)}${unit ? ` ${unit}` : ''}`;
const clock = (minute: number | null | undefined) =>
  minute == null
    ? '—'
    : `${Math.floor(minute / 60)
        .toString()
        .padStart(2, '0')}:${Math.floor(minute % 60)
        .toString()
        .padStart(2, '0')}`;

export function DynamicTrackingDetails({
  regions,
  policy,
}: {
  regions: RegionalExecution[];
  policy?: DynamicTrackingPolicy | null;
}) {
  const rows = regions.flatMap((region) =>
    (['p', 'q'] as const).flatMap((axis) => {
      const assessment = region.dynamic_tracking?.[axis];
      return assessment ? [{ region: region.region, axis, assessment }] : [];
    }),
  );
  if (!rows.length) return null;
  return (
    <details className={styles.details}>
      <summary>分钟级动态跟踪 · 响应时间与稳定后偏差</summary>
      <p>
        全程峰值包含目标切换后的暂态。响应期限后继续检查偏差，设备与电网安全约束始终逐分钟校核。
      </p>
      {policy?.maximum_response_minutes != null &&
        policy.confirmation_samples != null && (
          <p>
            响应期限按每次变化计算，最长 {policy.maximum_response_minutes}{' '}
            分钟；连续 {policy.confirmation_samples}{' '}
            点确认。跨目标连续超限超过该最长时间仍判未通过。
          </p>
        )}
      <div className={styles.scroll}>
        <table>
          <caption>各区域动态跟踪结果</caption>
          <thead>
            <tr>
              <th>区域 / 功率</th>
              <th>全程最大偏差 / 时刻</th>
              <th>响应期限后最大偏差</th>
              <th>偏差限值</th>
              <th>响应 / 稳定后</th>
            </tr>
          </thead>
          <tbody>
            {rows.map(({ region, axis, assessment: a }) => (
              <tr key={`${region}-${axis}`}>
                <th scope="row">
                  {region} / {axis.toUpperCase()}
                </th>
                <td>
                  {value(a.raw_max_error, a.unit)} /{' '}
                  {clock(a.raw_max_error_time_minute)}
                </td>
                <td>{value(a.post_deadline_max_error, a.unit)}</td>
                <td>{value(a.limit, a.unit)}</td>
                <td>
                  {statusLabel[a.response_status]} /{' '}
                  {statusLabel[a.steady_status]}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      {rows.map(({ region, axis, assessment: a }) => (
        <details key={`${region}-${axis}`} className={styles.region}>
          <summary>
            {region} · {axis.toUpperCase()} · {a.transitions.length} 段目标 ·{' '}
            {statusLabel[a.status]}
          </summary>
          <p>
            全程 RMSE：{value(a.rmse, a.unit)}；连续超限最长{' '}
            {a.longest_outside_band_minutes} 分钟；无效采样 {a.invalid_samples}{' '}
            点。
          </p>
          <div className={styles.scroll}>
            <table>
              <caption>
                {region} {axis.toUpperCase()} 目标切换与响应明细
              </caption>
              <thead>
                <tr>
                  <th>目标生效</th>
                  <th>允许响应</th>
                  <th>进入偏差带</th>
                  <th>连续确认完成</th>
                  <th>期限后最大偏差</th>
                  <th>结论</th>
                </tr>
              </thead>
              <tbody>
                {a.transitions.map((t) => (
                  <tr key={t.start_minute}>
                    <th scope="row">{clock(t.start_minute)}</th>
                    <td>{value(t.allowed_response_minutes, '分钟')}</td>
                    <td>{value(t.entered_band_after_minutes, '分钟')}</td>
                    <td>{value(t.confirmed_after_minutes, '分钟')}</td>
                    <td>{value(t.post_deadline_max_error, a.unit)}</td>
                    <td>{t.reason}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </details>
      ))}
      <p>
        耗时从目标生效起算；分钟曲线在每步响应后采样，仍以该步起始时刻标记。动态判据为软件仿真设置。
      </p>
    </details>
  );
}
