'use client';

import { useState } from 'react';
import { Play, Zap } from 'lucide-react';
import type {
  ADMMHistoryPoint,
  ClusterTimeSeriesPoint,
} from '@/shared/api/generated/coordination';
import { REGION_DEFINITIONS } from '@/shared/lib/region-presentation';
import type { ClusterExecution } from '@/shared/api/generated/cluster-execution';
import { CoordinationRegionCard } from './region-card';
import { CoordinationTopologyRegion } from './topology-region';
import { ExecutionEvidence } from './execution-panel';
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
  execution,
}: {
  mode: CoordinationMode;
  onModeChange?: (mode: CoordinationMode) => void;
  record?: ADMMHistoryPoint;
  planPoint?: ClusterTimeSeriesPoint;
  hasResult: boolean;
  busy: boolean;
  converged: boolean | null;
  canReplay: boolean;
  onReplay: () => void;
  playing: boolean;
  execution?: ClusterExecution | null;
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
      <div className={`panel-title ${styles.heading}`}>
        <h3>跨区域微电网集群</h3>
        <em>
          {busy
            ? hasResult
              ? '计算中 · 当前显示上次结果'
              : '正在计算协调结果'
            : hasResult
              ? convergenceLabel(converged)
              : '等待运行集群协调'}
        </em>
      </div>
      {(onModeChange || process) && (
        <div className={styles.toolbar}>
          {onModeChange && (
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
          )}
          {process && times.length > 0 && (
            <label className={styles.timePicker}>
              观察计划时刻
              <select
                aria-label="观察计划时刻"
                value={times[slot]}
                onChange={(event) =>
                  setObservedHour(Number(event.target.value))
                }
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
      )}
      {execution && <ExecutionEvidence execution={execution} />}
      <div className={styles.canvasScroll}>
        <div className={`topology-canvas ${styles.canvas}`}>
          <div className="grid-source">
            <Zap size={25} />
            <span>上级电网 / 协调层</span>
            <b>
              {process
                ? 'ADMM 共识'
                : planPoint
                  ? powerLabel(planPoint.aggregate_import_mw, 'MW')
                  : '运行后显示'}
            </b>
          </div>
          <div className="trunk trunk-main" aria-hidden="true" />
          <div className="trunk trunk-left" aria-hidden="true" />
          <div className="trunk trunk-right" aria-hidden="true" />
          {regionIds.map((id, index) => (
            <CoordinationTopologyRegion
              key={id}
              regionId={id}
              index={index}
              trace={trace?.regions.find((region) => region.region === id)}
              slot={slot}
              process={process}
              finalPower={planPoint?.regional_import_mw[id]}
              emptyLabel={emptyLabel}
            />
          ))}
          {playing && (
            <>
              <div className="flow-pulse pulse-1" aria-hidden="true" />
              <div className="flow-pulse pulse-2" aria-hidden="true" />
              <div className="flow-pulse pulse-3" aria-hidden="true" />
            </>
          )}
        </div>
      </div>
      {emptyLabel && hasResult && (
        <p className={styles.notice}>
          {emptyLabel}。可切换“{onModeChange ? '最终计划' : '运行态势'}
          ”查看已保存的 PCC 参考。
        </p>
      )}
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
      <details className={styles.details}>
        <summary>
          协调详情{hour == null ? '' : ` · 计划时刻 ${planTimeLabel(hour)}`}
        </summary>
        <p className={styles.note}>
          {process
            ? record
              ? `第 ${record.iteration} 轮 · ${trace ? (trace.global_updated ? '已更新协调参考' : '等待有效区域响应，参考保持') : '原生迭代记录'}`
              : '协调结果将在运行后显示'
            : '各区域日内 PCC 参考计划'}
        </p>
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
      </details>
      <p className={styles.note}>
        {process
          ? '底部按协调轮次回放；观察时刻用于选取每轮全天计划中的同一时段。'
          : '底部按原始计划时刻回放，不插值。'}{' '}
        PCC 正值为受电；协调参考不代表设备执行实绩。
      </p>
    </section>
  );
}
