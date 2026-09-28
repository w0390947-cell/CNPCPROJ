'use client';

import { useId, useMemo } from 'react';
import {
  Area,
  CartesianGrid,
  ComposedChart,
  Line,
  ReferenceLine,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from 'recharts';
import type { TimeSeriesPoint } from '../../shared/api/generated/timeseries';
import { activePowerPoints, powerDetails, powerLabel } from './presentation';
import styles from './style.module.css';

const series = [
  { label: '负荷', color: '#a4afff', kind: 'line' },
  { label: 'PCC 受电', color: '#39c9ff', kind: 'line' },
  { label: '风电', color: '#42c99a', kind: 'area' },
  { label: '光伏', color: '#f3c95d', kind: 'area' },
  { label: '储能放电', color: '#ad8cff', kind: 'area' },
  { label: '储能充电（零轴下方）', color: '#f79768', kind: 'area' },
];

export function ActivePowerChart({
  points,
  mounted,
  running,
}: {
  points: readonly TimeSeriesPoint[];
  mounted: boolean;
  running: boolean;
}) {
  const data = useMemo(() => activePowerPoints(points), [points]);
  const titleId = useId();
  const noteId = useId();
  const incomplete = data.some((p) =>
    powerDetails(p).some((item) => item.value === null),
  );
  return (
    <section
      className={`panel chart-panel ${styles.panel}`}
      aria-labelledby={titleId}
    >
      <div className={styles.heading}>
        <h3 id={titleId}>负荷、风光储与 PCC 受电</h3>
        <span className={styles.badge}>有功计划 · MW</span>
      </div>
      <ul className={styles.legend} aria-label="有功功率图例">
        {series.map((item) => (
          <li key={item.label}>
            <i
              className={item.kind === 'line' ? styles.lineKey : styles.areaKey}
              style={{ backgroundColor: item.color }}
              aria-hidden="true"
            />
            {item.label}
          </li>
        ))}
      </ul>
      <div className={styles.chart} aria-describedby={noteId}>
        {mounted && data.length > 0 ? (
          <ResponsiveContainer width="100%" height="100%">
            <ComposedChart
              accessibilityLayer
              data={data}
              margin={{ top: 20, right: 12, bottom: 4, left: 0 }}
            >
              <CartesianGrid
                stroke="#17384b"
                strokeDasharray="4 6"
                vertical={false}
              />
              <XAxis
                dataKey="time"
                stroke="#92aebf"
                tickLine={false}
                axisLine={false}
                fontSize={11}
                minTickGap={28}
              />
              <YAxis
                stroke="#92aebf"
                tickLine={false}
                axisLine={false}
                fontSize={11}
                width={48}
                domain={['auto', 'auto']}
                label={{
                  value: 'MW',
                  position: 'insideTopLeft',
                  dy: -18,
                  fill: '#92aebf',
                }}
              />
              <ReferenceLine y={0} stroke="#7695a8" strokeDasharray="3 3" />
              <Tooltip
                filterNull={false}
                content={({ active, label }) => {
                  const point = active
                    ? data.find((p) => p.time === label)
                    : undefined;
                  if (!point) return null;
                  return (
                    <div className={styles.tooltip}>
                      <strong>{point.time} · 有功计划</strong>
                      <dl>
                        {powerDetails(point).map((item) => (
                          <div key={item.label}>
                            <dt>{item.label}</dt>
                            <dd>{powerLabel(item.value)}</dd>
                          </div>
                        ))}
                      </dl>
                      <p>充电数值为吸收功率，图中向下绘制。</p>
                    </div>
                  );
                }}
              />
              <Area
                name="风电"
                type="linear"
                dataKey="windArea"
                stackId="localSupply"
                stroke="#42c99a"
                fill="#42c99a"
                fillOpacity={0.25}
                connectNulls={false}
                isAnimationActive={false}
              />
              <Area
                name="光伏"
                type="linear"
                dataKey="pvArea"
                stackId="localSupply"
                stroke="#f3c95d"
                fill="#f3c95d"
                fillOpacity={0.25}
                connectNulls={false}
                isAnimationActive={false}
              />
              <Area
                name="储能放电"
                type="linear"
                dataKey="dischargeArea"
                stackId="localSupply"
                stroke="#ad8cff"
                fill="#ad8cff"
                fillOpacity={0.3}
                connectNulls={false}
                isAnimationActive={false}
              />
              <Area
                name="储能充电"
                type="linear"
                dataKey="chargeBelowZero"
                baseValue={0}
                stroke="#f79768"
                fill="#f79768"
                fillOpacity={0.25}
                connectNulls={false}
                isAnimationActive={false}
              />
              <Line
                name="负荷"
                type="linear"
                dataKey="load"
                stroke="#a4afff"
                strokeWidth={2}
                strokeDasharray="6 3"
                dot={data.length === 1}
                connectNulls={false}
                isAnimationActive={false}
              />
              <Line
                name="PCC 受电"
                type="linear"
                dataKey="import"
                stroke="#39c9ff"
                strokeWidth={2.5}
                dot={data.length === 1}
                connectNulls={false}
                isAnimationActive={false}
              />
            </ComposedChart>
          </ResponsiveContainer>
        ) : (
          <div className={styles.empty}>
            <strong>{running ? '正在计算本场景' : '运行后呈现算法轨迹'}</strong>
            <p>完成仿真后显示负荷、风光储与受电计划。</p>
          </div>
        )}
      </div>
      <p className={styles.note} id={noteId}>
        堆叠面积表示风电、光伏与储能放电的本地供电构成；储能充电向下绘制。
        悬停查看各项功率及网损。曲线为优化计划采样点连线。
      </p>
      {incomplete && (
        <p className={styles.missing} role="note">
          部分计划数据缺失，明细显示“—”；本地供电分项不完整时，该时刻堆叠面积留空。
        </p>
      )}
    </section>
  );
}
