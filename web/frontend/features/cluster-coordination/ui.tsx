'use client';

import { useState } from 'react';
import { Network, Play } from 'lucide-react';
import type {
  ADMMHistoryPoint,
  ClusterTimeSeriesPoint,
} from '@/shared/api/generated/coordination';
import { REGION_DEFINITIONS } from '@/shared/lib/region-presentation';
import { CoordinationRegionCard } from './region-card';
import {
  convergenceLabel,
  planTimeLabel,
  powerLabel,
  type CoordinationMode,
} from './presentation';
import styles from './style.module.css';

export function ClusterCoordinationPanel({
  mode,
  onModeChange,
  record,
  planPoint,
  hasResult,
  busy,
  converged,
  canReplay,
  onReplay,
  playing,
}: {
  mode: CoordinationMode;
  onModeChange: (mode: CoordinationMode) => void;
  record?: ADMMHistoryPoint;
  planPoint?: ClusterTimeSeriesPoint;
  hasResult: boolean;
  busy: boolean;
  converged: boolean | null;
  canReplay: boolean;
  onReplay: () => void;
  playing: boolean;
}) {
  const [observedHour, setObservedHour] = useState<number | null>(null);
  const process = mode === 'process';
  const trace = record?.coordination;
  const times = trace?.time_hours ?? [];
  const slot = Math.max(
    0,
    times.findIndex((hour) => hour === observedHour),
  );
  const hour = process ? times[slot] : planPoint?.time_hour;
  const emptyLabel = !hasResult
    ? busy
      ? '计算中'
      : '运行后显示'
    : process && !record
      ? '迭代记录缺失'
      : process && !trace
        ? '本次结果未记录逐轮功率'
        : null;
  const regionIds =
    trace?.regions.map((region) => region.region) ??
    (planPoint
      ? Object.keys(planPoint.regional_import_mw)
      : REGION_DEFINITIONS.map((region) => region.id));
  const count = regionIds.length;
  const ratio =
    planPoint && planPoint.import_limit_mw > 0
      ? (planPoint.aggregate_import_mw / planPoint.import_limit_mw) * 100
      : null;

  return (
    <section
      className={`panel topology-panel ${styles.panel}`}
      aria-label="跨区域微电网集群"
    >
      <div className={styles.heading}>
        <h3>跨区域微电网集群</h3>
        <output>
          {busy
            ? hasResult
              ? '计算中 · 当前显示上次结果'
              : '正在计算协调结果'
            : hasResult
              ? convergenceLabel(converged)
              : '等待运行集群协调'}
        </output>
      </div>
      <div className={styles.toolbar}>
        <fieldset className={styles.modes} aria-label="集群展示模式">
          <button
            type="button"
            aria-pressed={process}
            disabled={busy}
            onClick={() => onModeChange('process')}
          >
            协调过程
          </button>
          <button
            type="button"
            aria-pressed={!process}
            disabled={busy}
            onClick={() => onModeChange('plan')}
          >
            最终计划
          </button>
        </fieldset>
        {process && times.length > 0 && (
          <label className={styles.timePicker}>
            观察计划时刻
            <select
              aria-label="观察计划时刻"
              value={times[slot]}
              onChange={(event) => setObservedHour(Number(event.target.value))}
            >
              {times.map((time) => (
                <option key={time} value={time}>
                  {planTimeLabel(time)}
                </option>
              ))}
            </select>
          </label>
        )}
        <button
          type="button"
          className={styles.replay}
          disabled={!canReplay || busy}
          onClick={onReplay}
        >
          <Play size={14} />
          回放协调过程
        </button>
      </div>
      <div className={styles.coordinator} data-playing={playing || undefined}>
        <Network size={20} aria-hidden="true" />
        <div>
          <strong>上级电网 / 协调层</strong>
          <p>
            {process
              ? record
                ? `第 ${record.iteration} 轮 · ${trace ? (trace.global_updated ? '已更新协调参考' : '等待有效区域响应，参考保持') : '原生迭代记录'}`
                : '协调结果将在运行后显示'
              : '各区域日内 PCC 参考计划'}
          </p>
        </div>
        <b>{hour == null ? '' : `计划时刻 ${planTimeLabel(hour)}`}</b>
      </div>
      {emptyLabel && hasResult && (
        <p className={styles.notice}>
          {emptyLabel}。可切换“最终计划”查看已保存的 PCC 参考。
        </p>
      )}
      <div className={styles.regions}>
        {regionIds.map((id) => (
          <CoordinationRegionCard
            key={id}
            regionId={id}
            trace={trace?.regions.find((region) => region.region === id)}
            slot={slot}
            process={process}
            finalPower={planPoint?.regional_import_mw[id]}
            emptyLabel={emptyLabel}
          />
        ))}
      </div>
      <div className={styles.capacity}>
        <span>
          {process ? '当前有效区域信息' : 'PCC 受电 / 本场景通道上限'}
        </span>
        <strong>
          {!hasResult
            ? '运行后显示'
            : process
              ? record
                ? `${record.fresh_region_count} / ${count}`
                : '迭代记录缺失'
              : planPoint
                ? `${powerLabel(planPoint.aggregate_import_mw, 'MW')} / ${powerLabel(planPoint.import_limit_mw, 'MW')}`
                : '计划数据缺失'}
        </strong>
        {!process && ratio != null && (
          <progress
            aria-label="PCC 通道占用"
            max={100}
            value={Math.min(100, Math.max(0, ratio))}
          />
        )}
      </div>
      <p className={styles.note}>
        {process
          ? '底部按协调轮次回放；观察时刻用于选取每轮全天计划中的同一时段。'
          : '底部按原始计划时刻回放，不插值。'}{' '}
        PCC 正值为受电；协调参考不代表设备执行实绩。
      </p>
    </section>
  );
}
