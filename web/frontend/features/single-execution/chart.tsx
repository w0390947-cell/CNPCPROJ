'use client';

import { useId } from 'react';
import styles from './style.module.css';

export type Curve = {
  label: string;
  color: string;
  values: Array<number | null>;
  dashed?: boolean;
};
export function EvidenceChart({
  title,
  minutes,
  curves,
  selected,
  onSelect,
  bounds = [],
  markers = [],
}: {
  title: string;
  minutes: number[];
  curves: Curve[];
  selected: number;
  onSelect: (minute: number) => void;
  bounds?: number[];
  markers?: Array<{ minute: number; label: string }>;
}) {
  const id = useId();
  const values = [
    ...bounds,
    ...curves.flatMap((c) =>
      c.values.filter((v): v is number => v != null && Number.isFinite(v)),
    ),
  ];
  if (!values.length) return <p>{title}：未记录可绘制数据。</p>;
  let low = Math.min(...values),
    high = Math.max(...values);
  const margin = Math.max((high - low) * 0.08, 0.01);
  low -= margin;
  high += margin;
  const left = 80,
    right = 930,
    top = 24,
    bottom = 215;
  const end = Math.max(1, minutes.at(-1) ?? 1);
  const x = (minute: number) => left + (minute / end) * (right - left);
  const y = (v: number) => bottom - ((v - low) / (high - low)) * (bottom - top);
  return (
    <figure className={styles.chart}>
      <figcaption id={id}>{title}</figcaption>
      <button
        type="button"
        className={styles.chartButton}
        aria-label={`${title}，左右方向键定位周期`}
        onKeyDown={(event) => {
          if (event.key === 'ArrowLeft' || event.key === 'ArrowRight') {
            event.preventDefault();
            onSelect(
              Math.max(
                0,
                Math.min(end, selected + (event.key === 'ArrowLeft' ? -1 : 1)),
              ),
            );
          }
        }}
        onClick={(event) => {
          if (!event.detail) return;
          const rect = event.currentTarget.getBoundingClientRect();
          if (!rect.width) return;
          const target = Math.max(
            0,
            Math.min(
              end,
              ((((event.clientX - rect.left) / rect.width) * 960 - left) /
                (right - left)) *
                end,
            ),
          );
          const nearest = minutes.reduce(
            (a, b) => (Math.abs(b - target) < Math.abs(a - target) ? b : a),
            minutes[0] ?? 0,
          );
          onSelect(nearest);
        }}
      >
        <svg viewBox="0 0 960 255" aria-labelledby={id}>
          {[0, 0.25, 0.5, 0.75, 1].map((v) => (
            <g key={v}>
              <line
                x1={left}
                x2={right}
                y1={top + v * (bottom - top)}
                y2={top + v * (bottom - top)}
                stroke="var(--plot-grid, #294256)"
              />
              <text
                x={left - 8}
                y={top + v * (bottom - top) + 4}
                textAnchor="end"
              >
                {(high - v * (high - low)).toFixed(3)}
              </text>
              <text x={x(v * end)} y="240" textAnchor="middle">
                {Math.round(v * end)} min
              </text>
            </g>
          ))}
          {curves.map((c) => {
            let pen = false;
            const path = c.values
              .map((v, i) => {
                if (v == null || !Number.isFinite(v)) {
                  pen = false;
                  return '';
                }
                const cmd = pen ? 'L' : 'M';
                pen = true;
                return `${cmd}${x(minutes[i]).toFixed(2)},${y(v).toFixed(2)}`;
              })
              .join(' ');
            return (
              <path
                key={c.label}
                d={path}
                fill="none"
                stroke={c.color}
                strokeWidth="2"
                strokeDasharray={c.dashed ? '6 4' : undefined}
              >
                <title>{c.label}</title>
              </path>
            );
          })}
          <line
            x1={x(selected)}
            x2={x(selected)}
            y1={top}
            y2={bottom}
            stroke="var(--plot-cursor, #fff)"
            strokeDasharray="3 3"
          />
          {markers
            .filter((m) => m.minute <= end)
            .map((m, i) => (
              <g key={m.label}>
                <line
                  x1={x(m.minute)}
                  x2={x(m.minute)}
                  y1={top}
                  y2={bottom}
                  stroke="var(--plot-orange, #ffbf79)"
                  strokeDasharray="8 4"
                />
                <text
                  x={Math.min(right - 8, Math.max(left + 8, x(m.minute)))}
                  y={top + 12 + i * 16}
                  textAnchor={m.minute / end > 0.8 ? 'end' : 'start'}
                >
                  {m.label} {m.minute}
                </text>
              </g>
            ))}
        </svg>
      </button>
      <ul className={styles.legend}>
        {curves.map((c) => (
          <li key={c.label}>
            <i style={{ background: c.color }} />
            {c.label}
          </li>
        ))}
      </ul>
    </figure>
  );
}
