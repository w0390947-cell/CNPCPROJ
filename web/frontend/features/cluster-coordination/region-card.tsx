import type { RegionalCoordinationTrace } from '@/shared/api/generated/coordination';
import { regionDisplayName } from '@/shared/lib/region-presentation';
import { informationStatus, powerLabel } from './presentation';
import styles from './style.module.css';

export function CoordinationRegionCard({
  regionId,
  trace,
  slot,
  finalPower,
  process,
  emptyLabel,
}: {
  regionId: string;
  trace?: RegionalCoordinationTrace;
  slot: number;
  finalPower?: number;
  process: boolean;
  emptyLabel: string | null;
}) {
  const proposal = trace?.proposal_p_mw?.[slot];
  const reference = trace?.reference_p_mw[slot];
  const difference =
    proposal != null && reference != null ? proposal - reference : null;
  const faulted = process && (trace?.outage || trace?.fallback);
  return (
    <article
      className={styles.region}
      data-region={regionId}
      data-faulted={faulted || undefined}
    >
      <h4>{regionDisplayName(regionId)}</h4>
      <p className={styles.regionStatus}>
        {emptyLabel ??
          (process ? informationStatus(trace) : '风 · 光 · 储 / 协调参考')}
      </p>
      <dl>
        {process && (
          <div>
            <dt>{trace?.fresh ? '本轮区域申报' : '最近收到的申报'}</dt>
            <dd>
              {emptyLabel ??
                (trace?.proposal_p_mw == null
                  ? '尚未收到申报'
                  : powerLabel(proposal, 'MW'))}
            </dd>
          </div>
        )}
        <div className={styles.reference}>
          <dt>{process ? '协调器参考 P' : 'PCC 计划'}</dt>
          <dd>
            {emptyLabel ?? powerLabel(process ? reference : finalPower, 'MW')}
          </dd>
        </div>
        {process && (
          <div>
            <dt>申报 − 参考</dt>
            <dd>{emptyLabel ?? powerLabel(difference, 'MW')}</dd>
          </div>
        )}
      </dl>
      {process && trace && !emptyLabel && (
        <details>
          <summary>无功与信息来源</summary>
          <dl>
            <div>
              <dt>区域申报 Q</dt>
              <dd>
                {trace.proposal_q_mvar == null
                  ? '尚未收到申报'
                  : powerLabel(trace.proposal_q_mvar[slot], 'Mvar')}
              </dd>
            </div>
            <div>
              <dt>协调器参考 Q</dt>
              <dd>{powerLabel(trace.reference_q_mvar[slot], 'Mvar')}</dd>
            </div>
          </dl>
          <p>
            申报发送轮次：{trace.response_sent_tick ?? '尚未收到'}；协调批次：
            {trace.response_epoch ?? '尚未收到'}
          </p>
        </details>
      )}
    </article>
  );
}
