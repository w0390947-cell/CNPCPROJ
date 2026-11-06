'use client';
import { useId, useState } from 'react';
import type { InputMetric, InputRange, InputRegion } from '../lib/input-series';
import styles from './input-curves.module.css';

const format = (v: number) =>
  v.toLocaleString('zh-CN', { maximumFractionDigits: 3 });
const rangeText = (v?: InputRange | null) =>
  v == null
    ? '未记录'
    : v.low === v.high
      ? format(v.low)
      : `${format(v.low)}—${format(v.high)}`;

function InputChart({
  metric,
  total,
  minute,
  completed,
  onSelect,
}: {
  metric: InputMetric;
  total: number;
  minute: number;
  completed: number;
  onSelect?: (n: number) => void;
}) {
  const id = useId();
  const observed = metric.observed.map((v, i) => (i < completed ? v : null));
  const values = [
    ...metric.forecast.filter((v): v is number => v != null),
    ...[...metric.declared, ...observed].flatMap((v) =>
      v ? [v.low, v.high] : [],
    ),
  ];
  const low = Math.min(0, ...values),
    high = Math.max(0.01, ...values);
  const pad = Math.max((high - low) * 0.1, 0.01);
  const x = (t: number) => 60 + (t / Math.max(1, total)) * 550;
  const y = (v: number) =>
    185 - ((v - low + pad) / (high - low + pad * 2)) * 160;
  const current = Math.min(total - 1, Math.max(0, minute - 1));
  const line = (rows: (number | null)[]) =>
    rows
      .map((v, i) =>
        v == null
          ? ''
          : `${i > 0 && rows[i - 1] != null ? 'L' : 'M'}${x(i)},${y(v)} H${x(i + 1)}`,
      )
      .join(' ');
  const band = (rows: (InputRange | null)[]) => {
    let path = '',
      start = 0;
    while (start < rows.length) {
      if (!rows[start]) {
        start++;
        continue;
      }
      let end = start;
      while (end + 1 < rows.length && rows[end + 1]) end++;
      path += `M${x(start)},${y(rows[start]!.low)} `;
      for (let i = start; i <= end; i++)
        path += `L${x(i)},${y(rows[i]!.low)} H${x(i + 1)} `;
      for (let i = end; i >= start; i--)
        path += `L${x(i + 1)},${y(rows[i]!.high)} H${x(i)} `;
      path += 'Z ';
      start = end + 1;
    }
    return path;
  };
  const select = (step: number) =>
    onSelect?.(Math.max(0, Math.min(completed, step)));
  const interactive = !!onSelect && completed > 0;
  return (
    <figure className={styles.chart} aria-label={`${metric.title}输入曲线`}>
      <figcaption id={id}>
        {metric.title} <span>/ {metric.unit}</span>
      </figcaption>
      <button
        type="button"
        className={styles.plot}
        disabled={!interactive}
        aria-label={`${metric.title}输入曲线，当前完成节点 ${minute} min，左右键选择已完成区间`}
        onKeyDown={(e) => {
          if (e.key === 'ArrowLeft' || e.key === 'ArrowRight') {
            e.preventDefault();
            select(minute + (e.key === 'ArrowLeft' ? -1 : 1));
          }
        }}
        onClick={(e) => {
          if (!e.detail) return;
          const rect = e.currentTarget.getBoundingClientRect();
          if (!rect.width) return;
          const t =
            ((((e.clientX - rect.left) / rect.width) * 640 - 60) / 550) * total;
          select(t < 0 ? 0 : Math.floor(t) + 1);
        }}
      >
        <svg viewBox="0 0 640 225" aria-labelledby={id}>
          {[0, 0.5, 1].map((f) => (
            <g key={f}>
              <line
                x1="60"
                x2="610"
                y1={y(low + (high - low) * f)}
                y2={y(low + (high - low) * f)}
                className={styles.grid}
              />
              <text x="52" y={y(low + (high - low) * f) + 4} textAnchor="end">
                {format(low + (high - low) * f)}
              </text>
            </g>
          ))}
          {[0, 0.25, 0.5, 0.75, 1].map((f) => (
            <text key={f} x={x(total * f)} y="210" textAnchor="middle">
              {format(total * f)} min
            </text>
          ))}
          {completed < total && (
            <rect
              x={x(completed)}
              y="20"
              width={x(total) - x(completed)}
              height="170"
              className={styles.future}
            />
          )}
          {!metric.lowerOnly && (
            <>
              <path
                d={band(metric.declared)}
                className={styles.band}
                data-series="declared-range"
              />
              <path
                d={line(metric.declared.map((v) => v?.low ?? null))}
                className={styles.bandBoundary}
                vectorEffect="non-scaling-stroke"
                data-series="declared-lower"
              />
              <path
                d={line(metric.declared.map((v) => v?.high ?? null))}
                className={styles.bandBoundary}
                vectorEffect="non-scaling-stroke"
                data-series="declared-upper"
              />
            </>
          )}
          {metric.lowerOnly && (
            <path
              d={line(metric.declared.map((v) => v?.low ?? null))}
              className={styles.lower}
              data-series="declared-minimum"
            />
          )}
          <path
            d={line(metric.forecast)}
            className={styles.forecast}
            data-series="forecast"
          />
          <path d={band(observed)} className={styles.observedBand} />
          <path
            d={line(observed.map((v) => v?.low ?? null))}
            className={styles.observed}
            data-series="observed"
          />
          <path
            d={line(observed.map((v) => v?.high ?? null))}
            className={styles.observed}
          />
          {minute > 0 && (
            <>
              <rect
                x={x(current)}
                y="20"
                width={Math.max(1, x(current + 1) - x(current))}
                height="170"
                className={styles.selection}
              />
              <line
                x1={x(minute)}
                x2={x(minute)}
                y1="20"
                y2="190"
                className={styles.cursor}
              />
            </>
          )}
        </svg>
      </button>
      <dl className={styles.values}>
        <div>
          <dt>预测</dt>
          <dd>
            {metric.forecast[current] == null
              ? '未记录'
              : format(metric.forecast[current]!)}
          </dd>
        </div>
        <div>
          <dt>{metric.lowerOnly ? '声明最低可用' : '声明范围'}</dt>
          <dd>
            {metric.lowerOnly && metric.declared[current]
              ? `≥ ${format(metric.declared[current]!.low)}`
              : rangeText(metric.declared[current])}
          </dd>
        </div>
        <div>
          <dt>观测</dt>
          <dd>{minute === 0 ? '尚未执行' : rangeText(observed[current])}</dd>
        </div>
      </dl>
    </figure>
  );
}

export type InputCurvesProps = {
  regions: InputRegion[];
  minute?: number;
  completed?: number;
  onSelect?: (n: number) => void;
  decision?: string;
  response?: string;
  onPlanning?: () => void;
  onResponse?: () => void;
};
export function InputCurves({
  regions,
  minute = 0,
  completed = 0,
  onSelect,
  decision,
  response,
  onPlanning,
  onResponse,
}: InputCurvesProps) {
  const [name, setName] = useState('');
  const region = regions.find((r) => r.name === name) ?? regions[0];
  if (!region) return <p>当前记录未提供输入曲线数据。</p>;
  const cursor = Math.max(0, Math.min(minute, completed, region.total));
  const hasForecast = region.metrics.some((m) =>
    m.forecast.some((v) => v != null),
  );
  const hasLoadRange = region.metrics.some(
    (m) => !m.lowerOnly && m.declared.some((v) => v != null),
  );
  const hasGenerationMinimum = region.metrics.some(
    (m) => m.lowerOnly && m.declared.some((v) => v != null),
  );
  // Match the plotted history, including zero values, rather than the cursor's
  // current sample. Future or missing observations must not create a legend.
  const hasObserved = region.metrics.some((m) =>
    m.observed.some((v, i) => i < completed && v != null),
  );
  return (
    <section className={styles.panel} aria-label="外部输入与执行联动">
      <div className={styles.heading}>
        <h2>外部输入与执行联动</h2>
        {regions.length > 1 && (
          <label>
            区域{' '}
            <select
              aria-label="输入曲线区域"
              value={region.name}
              onChange={(e) => setName(e.target.value)}
            >
              {regions.map((r) => (
                <option key={r.name} value={r.name}>
                  {r.name}
                </option>
              ))}
            </select>
          </label>
        )}
      </div>
      <p>
        {region.name} ·{' '}
        {cursor > 0
          ? `当前输入对应 ${cursor - 1}→${cursor} min 执行区间；设备状态对应 ${cursor} min 完成节点。`
          : '执行前预览；当前数值为首个执行区间的预测及声明条件。'}
      </p>
      <div className={styles.legend} aria-label="输入曲线公共图例">
        {hasForecast && (
          <span>
            <i className={styles.forecastKey} />
            预测值
          </span>
        )}
        {hasLoadRange && (
          <span>
            <i className={styles.bandKey} />
            负荷声明范围
          </span>
        )}
        {hasGenerationMinimum && (
          <span>
            <i className={styles.lowerKey} />
            风光最低保证量
          </span>
        )}
        {hasObserved && (
          <span>
            <i className={styles.observedKey} />
            观测值
          </span>
        )}
      </div>
      <p className={styles.note}>
        横轴为运行时间。预测与声明条件覆盖完整时域；灰底时段尚无已完成执行记录。点击曲线或使用左右键联动已完成节点，风光可用量与设备实际出力分别展示。
      </p>
      <div className={styles.charts}>
        {region.metrics.map((metric) => (
          <InputChart
            key={`${region.name}:${metric.key}`}
            metric={metric}
            total={region.total}
            minute={cursor}
            completed={completed}
            onSelect={onSelect}
          />
        ))}
      </div>
      {!hasForecast && (
        <p className={styles.note}>
          本记录未保存原始预测曲线；仅展示已有声明条件和观测，不从区间中点反推预测。
        </p>
      )}
      {!region.metrics.some((m) => m.declared.some((v) => v != null)) && (
        <p className={styles.note}>
          当前仅有原始预测输入；执行声明范围将在实际执行域可用后展示。
        </p>
      )}
      <p className={styles.note}>
        若记录为区间则保留区间。曲线按执行周期保持，不在缺失观测之间补点。
      </p>
      {cursor > 0 && (
        <ol className={styles.chain} aria-label="输入决策响应对应关系">
          <li>
            <b>输入条件</b>
            <span>
              上方数值对应 {cursor - 1}→{cursor} min 区间。
            </span>
          </li>
          <li>
            <b>规划与采用决策</b>
            <span>
              {decision ?? '请查看所选节点对应的规划窗口与实际采用记录。'}
            </span>
            {onPlanning && (
              <button type="button" onClick={onPlanning}>
                查看对应规划
              </button>
            )}
          </li>
          <li>
            <b>设备响应</b>
            <span>
              {response ??
                `查看 ${cursor} min 完成节点的设备出力、储能电量与 PCC 响应。`}
            </span>
            {onResponse && (
              <button type="button" onClick={onResponse}>
                查看对应响应
              </button>
            )}
          </li>
        </ol>
      )}
    </section>
  );
}
