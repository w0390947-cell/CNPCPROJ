'use client';

import { SunMedium, Wind } from 'lucide-react';
import { useId, useState } from 'react';

import type { EventType } from '@/shared/api/generated/study-events';
import {
  REGION_DEFINITIONS,
  regionDisplayName,
  type RegionCode,
} from '@/shared/lib/region-presentation';

import styles from './style.module.css';

export type RenewableSurgeKind = Extract<EventType, 'pv_surge' | 'wind_surge'>;

type RenewableEventControlsProps = {
  scope: 'cluster' | 'single';
  resultRegion: RegionCode;
  timeLabel: string;
  disabledReason: string | null;
  onInject: (kind: RenewableSurgeKind, target: RegionCode) => void;
};

/** Owns only the next event's target; the dashboard owns immutable result branches. */
export function RenewableEventControls({
  scope,
  resultRegion,
  timeLabel,
  disabledReason,
  onInject,
}: RenewableEventControlsProps) {
  const id = useId();
  const [selectedRegion, setSelectedRegion] = useState<RegionCode | null>(null);
  const target =
    scope === 'single' ? resultRegion : (selectedRegion ?? resultRegion);
  const disabled = disabledReason !== null;

  return (
    <section className={styles.controls} aria-label="新能源事件注入">
      <div className={styles.heading}>
        <h3>新能源事件注入</h3>
        <span>
          当前回放时刻 <b>{timeLabel}</b>
        </span>
      </div>
      <div className={styles.actions}>
        {scope === 'cluster' ? (
          <div className={styles.regionField}>
            <label htmlFor={`${id}-region`}>事件作用区域</label>
            <select
              id={`${id}-region`}
              value={target}
              disabled={disabled}
              aria-describedby={`${id}-help`}
              onChange={(event) => {
                const region = REGION_DEFINITIONS.find(
                  (item) => item.id === event.target.value,
                );
                if (region) setSelectedRegion(region.id);
              }}
            >
              {REGION_DEFINITIONS.map((region) => (
                <option key={region.id} value={region.id}>
                  {region.displayName}
                </option>
              ))}
            </select>
          </div>
        ) : (
          <p className={styles.fixedRegion}>
            事件作用区域 <strong>{regionDisplayName(target)}</strong>
          </p>
        )}
        <div className={styles.buttons}>
          <button
            type="button"
            disabled={disabled}
            aria-describedby={`${id}-help`}
            onClick={() => onInject('pv_surge', target)}
          >
            <SunMedium size={18} aria-hidden="true" />
            当前时刻注入光伏大发
          </button>
          <button
            type="button"
            disabled={disabled}
            aria-describedby={`${id}-help`}
            onClick={() => onInject('wind_surge', target)}
          >
            <Wind size={18} aria-hidden="true" />
            当前时刻注入风电大发
          </button>
        </div>
      </div>
      <p className={styles.help} id={`${id}-help`}>
        {disabledReason ??
          (scope === 'cluster'
            ? '区域选择仅作用于下一次事件；点击注入后，将沿用当前结果的参数生成新分支。'
            : '事件作用于当前结果的仿真区域；点击注入后，将沿用该结果的参数生成新分支。')}
      </p>
    </section>
  );
}
