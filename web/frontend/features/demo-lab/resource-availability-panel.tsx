import { Gauge, Info, TriangleAlert } from 'lucide-react';
import type { CSSProperties } from 'react';

import type {
  Catalog,
  Frame,
  ResourceAvailability,
} from '@/shared/api/generated/demo';

import { PanelHeading } from './panel-heading';
import { deviceKindLabels, displayMeasurement } from './presentation';
import { RenewableAvailabilityChart } from './renewable-availability-chart';

function finiteNumber(value: string) {
  const parsed = Number(value);
  return value.trim() !== '' && Number.isFinite(parsed) ? parsed : null;
}

function minuteOfDay(value: string) {
  const date = new Date(value);
  return date.getUTCHours() * 60 + date.getUTCMinutes();
}

function Metric({
  label,
  unit,
  value,
}: {
  label: string;
  unit?: string;
  value: number | null | undefined;
}) {
  return (
    <div>
      <dt>{label}</dt>
      <dd>
        {displayMeasurement(value)} {unit && <small>{unit}</small>}
      </dd>
    </div>
  );
}

function CapabilityRange({
  actual,
  draft,
  maximum,
  minimum,
  unit,
}: {
  actual: number;
  draft: number | null;
  maximum: number;
  minimum: number;
  unit: string;
}) {
  const width = maximum - minimum;
  const position = (value: number) =>
    width === 0
      ? 50
      : Math.max(0, Math.min(100, ((value - minimum) / width) * 100));
  const style = {
    '--actual-position': `${position(actual)}%`,
    '--draft-position': `${draft === null ? position(actual) : position(draft)}%`,
  } as CSSProperties;

  return (
    <div className="demo-capability-range" style={style}>
      <div aria-hidden="true" className="demo-capability-track">
        <i className="actual" />
        {draft !== null && <i className="draft" />}
      </div>
      <div className="demo-capability-labels">
        <span>
          {minimum.toFixed(2)} {unit}
        </span>
        <span>
          {maximum.toFixed(2)} {unit}
        </span>
      </div>
      <p>
        <i className="actual" /> 实际值
        {draft !== null && (
          <>
            <i className="draft" /> 输入目标
          </>
        )}
      </p>
    </div>
  );
}

export function ResourceAvailabilityPanel({
  availability,
  availabilityError,
  catalog,
  deviceId,
  frame,
  pDraft,
  qDraft,
}: {
  availability: ResourceAvailability | null;
  availabilityError: string;
  catalog: Catalog | null;
  deviceId: string;
  frame: Frame | null;
  pDraft: string;
  qDraft: string;
}) {
  const device = catalog?.devices.find((item) => item.device_id === deviceId);
  const reading = frame?.devices.find((item) => item.device_id === deviceId);
  const series = availability?.series.find(
    (item) => item.device_id === deviceId,
  );
  const draftP = finiteNumber(pDraft);
  const draftQ = finiteNumber(qDraft);
  const currentMinute = frame ? minuteOfDay(frame.simulated_at) : null;
  const renewable = device?.kind === 'wind' || device?.kind === 'pv';
  const exceedsAvailable =
    renewable &&
    reading &&
    draftP !== null &&
    draftP > reading.available_mw + 1e-9;

  return (
    <section
      aria-labelledby="resource-availability"
      className="panel demo-panel demo-resource-panel"
    >
      <PanelHeading
        headingId="resource-availability"
        icon={Gauge}
        title="当前资源与设备能力"
        trailing={
          device && (
            <strong className="demo-resource-device">
              {device.device_id} · {deviceKindLabels[device.kind]}
            </strong>
          )
        }
      />
      {!device || !reading ? (
        <div className="demo-resource-empty">
          <Gauge aria-hidden="true" />
          <span>等待所选设备的有效资料与遥测。</span>
        </div>
      ) : renewable ? (
        <>
          <dl className="demo-resource-metrics">
            <Metric label="当前可用" unit="MW" value={reading.available_mw} />
            <Metric label="实际出力" unit="MW" value={reading.p_mw} />
            <Metric label="输入目标" unit="MW" value={draftP} />
            <Metric
              label="当前余量"
              unit="MW"
              value={reading.available_mw - reading.p_mw}
            />
          </dl>
          {exceedsAvailable && (
            <output className="demo-resource-warning">
              <TriangleAlert aria-hidden="true" />
              输入目标高于当前可用功率，设备当前无法达到该值；命令是否受理及实际执行仍以后端为准。
            </output>
          )}
          {series && currentMinute !== null ? (
            <RenewableAvailabilityChart
              actual={reading.p_mw}
              available={reading.available_mw}
              currentMinute={currentMinute}
              deviceId={device.device_id}
              draft={draftP}
              points={series.points}
            />
          ) : (
            <div aria-live="polite" className="demo-resource-chart">
              <span>
                {availabilityError
                  ? `资源基准曲线暂不可用；当前可用功率和实际出力仍来自有效遥测。${availabilityError}`
                  : '正在读取资源曲线…'}
              </span>
            </div>
          )}
          <p className="demo-resource-note">
            <Info aria-hidden="true" />
            合成资料基准曲线 · 15 分钟分辨率 ·
            非实时天气预测。当前值包含故障影响，后端为最终权威。
          </p>
        </>
      ) : (
        <>
          <dl className="demo-resource-metrics">
            <Metric label="实际 P" unit="MW" value={reading.p_mw} />
            <Metric label="实际 Q" unit="Mvar" value={reading.q_mvar} />
            <Metric label="输入 P" unit="MW" value={draftP} />
            <Metric label="输入 Q" unit="Mvar" value={draftQ} />
            {device.kind === 'storage' && (
              <Metric label="当前电量" unit="MWh" value={reading.energy_mwh} />
            )}
          </dl>
          <CapabilityRange
            actual={device.kind === 'svg' ? reading.q_mvar : reading.p_mw}
            draft={device.kind === 'svg' ? draftQ : draftP}
            maximum={
              device.kind === 'svg' ? device.q_max_mvar : device.p_max_mw
            }
            minimum={
              device.kind === 'svg' ? -device.q_max_mvar : device.p_min_mw
            }
            unit={device.kind === 'svg' ? 'Mvar' : 'MW'}
          />
          <p className="demo-resource-note">
            <Info aria-hidden="true" />
            {device.kind === 'storage'
              ? `储能电量范围 ${(device.minimum_mwh ?? 0).toFixed(2)}—${(device.energy_mwh ?? 0).toFixed(2)} MWh；实际可达值还受能量、斜率和 P/Q/S 包络约束。`
              : `SVG 有功固定为 0 MW，声明无功范围为 ±${device.q_max_mvar.toFixed(2)} Mvar。`}{' '}
            命令校核和限幅以后端为准。
          </p>
        </>
      )}
    </section>
  );
}
