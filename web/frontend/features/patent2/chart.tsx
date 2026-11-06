'use client';
import { useId } from 'react';
import styles from './style.module.css';
export type Curve = {
  label: string;
  color: string;
  values: (number | null)[];
  dashed?: boolean;
};
export function Chart({
  title,
  curves,
  minute,
  onSelect,
  markers = [],
}: {
  title: string;
  curves: Curve[];
  minute: number;
  onSelect: (n: number) => void;
  markers?: number[];
}) {
  const id = useId();
  const all = curves.flatMap((c) =>
    c.values.filter((v): v is number => v != null && Number.isFinite(v)),
  );
  if (!all.length) return <p>{title}：未记录数据</p>;
  let lo = Math.min(...all),
    hi = Math.max(...all);
  const margin = Math.max((hi - lo) * 0.12, 0.01);
  lo -= margin;
  hi += margin;
  const end = Math.max(1, ...curves.map((c) => c.values.length - 1));
  const x = (n: number) => 65 + (n / end) * 800,
    y = (n: number) => 220 - ((n - lo) / (hi - lo)) * 190;
  return (
    <figure className={styles.chart}>
      <figcaption id={id}>{title}</figcaption>
      <button
        className={styles.chartControl}
        aria-label={`${title}，当前 ${minute} min，左右键切换分钟`}
        onKeyDown={(e) => {
          if (e.key === 'ArrowLeft' || e.key === 'ArrowRight') {
            e.preventDefault();
            onSelect(
              Math.max(
                0,
                Math.min(end, minute + (e.key === 'ArrowRight' ? 1 : -1)),
              ),
            );
          }
        }}
        onClick={(e) => {
          if (e.detail === 0) return;
          const r = e.currentTarget.getBoundingClientRect();
          onSelect(
            Math.max(
              0,
              Math.min(
                end,
                Math.round(
                  ((((e.clientX - r.left) / r.width) * 900 - 65) / 800) * end,
                ),
              ),
            ),
          );
        }}
      >
        <svg aria-labelledby={id} viewBox="0 0 900 260">
          {[0, 0.25, 0.5, 0.75, 1].map((t) => (
            <g key={t}>
              <line
                x1="65"
                x2="865"
                y1={y(lo + t * (hi - lo))}
                y2={y(lo + t * (hi - lo))}
                stroke="var(--plot-grid, #284354)"
              />
              <text x="58" y={y(lo + t * (hi - lo)) + 4} textAnchor="end">
                {(lo + t * (hi - lo)).toFixed(3)}
              </text>
              <text x={x(end * t)} y="246" textAnchor="middle">
                {Math.round(end * t)} min
              </text>
            </g>
          ))}
          {markers
            .filter((m) => m <= end)
            .map((m) => (
              <line
                key={m}
                x1={x(m)}
                x2={x(m)}
                y1="20"
                y2="220"
                stroke="var(--plot-orange, #f7bc73)"
                strokeDasharray="4 6"
              />
            ))}
          {curves.map((c) => {
            let pen = false;
            return (
              <path
                key={c.label}
                fill="none"
                stroke={c.color}
                strokeWidth="2.5"
                strokeDasharray={c.dashed ? '6 4' : undefined}
                d={c.values
                  .map((v, i) => {
                    if (v == null) {
                      pen = false;
                      return '';
                    }
                    const p = `${pen ? 'L' : 'M'}${x(i)},${y(v)}`;
                    pen = true;
                    return p;
                  })
                  .join(' ')}
              />
            );
          })}
          <line
            x1={x(minute)}
            x2={x(minute)}
            y1="20"
            y2="220"
            stroke="var(--plot-cursor, white)"
          />
        </svg>
      </button>
      <div className={styles.legend}>
        {curves.map((c) => (
          <span key={c.label}>
            <i style={{ background: c.color }} />
            {c.label}
          </span>
        ))}
      </div>
    </figure>
  );
}
