import { BatteryCharging, Network, SunMedium, Wind, Zap } from 'lucide-react';
import { numberLabel, type ProfileFrame } from '@/lib/simulation-presentation';
import { regionDisplayName } from '@/shared/lib/region-presentation';
import styles from './power-plan.module.css';

/** Functional groups show regional plan totals, not individual physical buses. */
export function SingleMicrogridCanvas({
  current,
  region,
}: {
  current: ProfileFrame;
  region: string;
}) {
  return (
    <div
      className="single-network"
      aria-label={`${regionDisplayName(region)}微电网功能分组示意`}
    >
      <div className="single-link link-pcc" />
      <div className="single-link link-wind" />
      <div className="single-link link-pv" />
      <div className="single-link link-flex" />
      <div className="single-device single-pcc">
        <Zap />
        <span>PCC 受电</span>
        <b>{numberLabel(current.import)} MW</b>
      </div>
      <div className="single-device single-main">
        <Network />
        <span>主母线</span>
        <b>全网负荷 {numberLabel(current.load)}</b>
      </div>
      <div className={`single-device single-wind ${styles.wind}`}>
        <Wind />
        <span>风电计划</span>
        <b className={styles.power}>
          P：{numberLabel(current.wind)} MW / Q：{numberLabel(current.windQ)} Mvar
        </b>
      </div>
      <div className="single-device single-pv">
        <SunMedium />
        <span>光伏计划</span>
        <b>{numberLabel(current.pv)} MW</b>
      </div>
      <div className="single-device single-flex">
        <BatteryCharging />
        <span>储能 / SVG</span>
        <b>
          {numberLabel(current.storage)} MW / {numberLabel(current.svg)} Mvar
        </b>
      </div>
    </div>
  );
}
