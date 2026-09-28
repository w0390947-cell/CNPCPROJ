import {
  Activity,
  BatteryCharging,
  Factory,
  Network,
  SunMedium,
  Wind,
} from 'lucide-react';
import type { RegionalCoordinationTrace } from '@/shared/api/generated/coordination';
import { regionDisplayName } from '@/shared/lib/region-presentation';
import { informationStatus, powerLabel } from './presentation';

/** The original topology card presents a reference, never an executed measurement. */
export function CoordinationTopologyRegion({
  regionId,
  index,
  trace,
  slot,
  finalPower,
  process,
  emptyLabel,
}: {
  regionId: string;
  index: number;
  trace?: RegionalCoordinationTrace;
  slot: number;
  finalPower?: number;
  process: boolean;
  emptyLabel: string | null;
}) {
  const faulted = process && (trace?.outage || trace?.fallback);
  const status = emptyLabel
    ? '待查看数据'
    : !process
      ? '计划回放'
      : faulted
        ? [trace?.outage && '失联', trace?.fallback && '自治降级']
            .filter(Boolean)
            .join(' · ')
        : !trace
          ? '未记录状态'
          : trace.fresh
            ? '有效信息'
            : trace?.response_epoch != null
              ? '历史申报'
              : '未收到申报';
  return (
    <article
      className={`region-node region-${index + 1} ${faulted ? 'faulted' : ''}`}
      data-region={regionId}
      data-faulted={faulted || undefined}
    >
      <div className="region-orbit" aria-hidden="true" />
      <div className="region-head">
        <div>
          <b>{regionDisplayName(regionId)}</b>
        </div>
        <span
          className={faulted ? 'fault-dot' : 'node-status'}
          title={
            emptyLabel ?? (process ? informationStatus(trace) : '最终计划回放')
          }
        >
          {status}
        </span>
      </div>
      <div className="region-flow" aria-hidden="true">
        <Wind size={17} />
        <SunMedium size={17} />
        <BatteryCharging size={17} />
        <i />
        <Factory size={18} />
      </div>
      <div className="region-stats">
        <span>
          控制资源<strong>风 · 光 · 储</strong>
        </span>
        <span>
          {process ? '协调器参考 P' : 'PCC 计划'}
          <strong>
            {emptyLabel
              ? emptyLabel === '本次结果未记录逐轮功率'
                ? '未记录逐轮功率'
                : emptyLabel
              : powerLabel(
                  process ? trace?.reference_p_mw[slot] : finalPower,
                  'MW',
                )}
          </strong>
        </span>
      </div>
      <footer>
        {faulted ? <Activity size={14} /> : <Network size={14} />}
        {faulted ? '通信故障回放' : '统一数据集区域模型'}
      </footer>
    </article>
  );
}
