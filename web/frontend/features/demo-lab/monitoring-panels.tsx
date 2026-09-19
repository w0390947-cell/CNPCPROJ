import {
  CheckCircle2,
  CircleX,
  Radio,
  ServerCog,
  ShieldCheck,
  TriangleAlert,
} from 'lucide-react';

import {
  Table,
  TableBody,
  TableCaption,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from '@/components/ui/table';
import type { Catalog, Frame } from '@/shared/api/generated/demo';
import { regionDisplayName } from '@/shared/lib/region-presentation';

import { PanelHeading } from './panel-heading';
import { displayMeasurement } from './presentation';

export function NetworkFeedbackPanel({ frame }: { frame: Frame | null }) {
  const blocked = frame?.recovery_blocked;
  return (
    <section
      aria-labelledby="network"
      className="panel demo-panel demo-network-panel"
    >
      <PanelHeading
        headingId="network"
        icon={ShieldCheck}
        title="实际出力的网络反馈"
        trailing={
          <strong
            className={`demo-state ${!frame ? 'unknown' : blocked ? 'danger' : 'healthy'}`}
          >
            {!frame ? (
              <TriangleAlert aria-hidden="true" />
            ) : blocked ? (
              <CircleX aria-hidden="true" />
            ) : (
              <CheckCircle2 aria-hidden="true" />
            )}
            {!frame
              ? '无法确认安全'
              : blocked
                ? '恢复动作已闭锁'
                : '恢复闭锁未触发'}
          </strong>
        }
      />
      <div className="demo-networks">
        {frame?.networks.map((network) => {
          const state = !network.valid
            ? 'unknown'
            : network.recovery_safe
              ? 'healthy'
              : 'warning';
          return (
            <article
              className="demo-network-card"
              data-state={state}
              key={network.region}
            >
              <div className="demo-network-heading">
                <h4>{regionDisplayName(network.region)}</h4>
                <span>{network.status}</span>
              </div>
              <dl>
                <div>
                  <dt>PCC 受电</dt>
                  <dd>
                    {displayMeasurement(network.pcc_import_mw)}{' '}
                    <small>MW</small>
                  </dd>
                </div>
                <div>
                  <dt>电压范围</dt>
                  <dd>
                    {displayMeasurement(network.voltage_min_pu)} —{' '}
                    {displayMeasurement(network.voltage_max_pu)}{' '}
                    <small>pu</small>
                  </dd>
                </div>
                <div>
                  <dt>最大支路负载率</dt>
                  <dd>
                    {displayMeasurement(network.max_loading_pu)}{' '}
                    <small>pu</small>
                  </dd>
                </div>
                <div>
                  <dt>网损</dt>
                  <dd>
                    {displayMeasurement(network.loss_mw)} <small>MW</small>
                  </dd>
                </div>
                <div>
                  <dt>功率因数</dt>
                  <dd>{displayMeasurement(network.power_factor)}</dd>
                </div>
              </dl>
              <p>
                {network.valid
                  ? network.recovery_safe
                    ? '电压、容量与防倒送恢复条件满足'
                    : '恢复条件不满足'
                  : '数据无效，无法确认安全'}
              </p>
              <details>
                <summary>支路潮流、电流与越限明细</summary>
                <pre>
                  {JSON.stringify(network.flow ?? network.reasons, null, 2)}
                </pre>
              </details>
            </article>
          );
        })}
        {!frame && (
          <div className="demo-empty-state">
            <Radio aria-hidden="true" />
            <strong>等待有效遥测</strong>
            <p>连接建立后显示各区域的潮流与安全判断。</p>
          </div>
        )}
      </div>
    </section>
  );
}

export function DeviceTelemetryPanel({ frame }: { frame: Frame | null }) {
  return (
    <section
      aria-labelledby="devices"
      className="panel demo-panel demo-device-panel"
    >
      <PanelHeading headingId="devices" icon={Radio} title="设备遥测" />
      {frame && !frame.quality_valid && (
        <p className="demo-inline-alert" role="alert">
          <TriangleAlert aria-hidden="true" />
          质量无效：下表仅显示模拟设备内部状态，不能作为有效现场量测。
        </p>
      )}
      <Table>
        <TableCaption className="sr-only">
          当前模拟设备的有功、无功、可用功率与储能电量
        </TableCaption>
        <TableHeader>
          <TableRow>
            {[
              '设备',
              '接入母线',
              'P / MW',
              'Q / Mvar',
              '可用有功 / MW',
              '储能电量 / MWh',
            ].map((heading) => (
              <TableHead key={heading}>{heading}</TableHead>
            ))}
          </TableRow>
        </TableHeader>
        <TableBody>
          {frame?.devices.map((device) => (
            <TableRow key={device.device_id}>
              <TableCell className="demo-device-id">
                {device.device_id}
              </TableCell>
              <TableCell>{device.bus_id}</TableCell>
              <TableCell>{displayMeasurement(device.p_mw)}</TableCell>
              <TableCell>{displayMeasurement(device.q_mvar)}</TableCell>
              <TableCell>{displayMeasurement(device.available_mw)}</TableCell>
              <TableCell>
                {device.energy_mwh === null
                  ? '—'
                  : displayMeasurement(device.energy_mwh)}
              </TableCell>
            </TableRow>
          ))}
          {!frame && (
            <TableRow>
              <TableCell className="demo-table-empty" colSpan={6}>
                等待有效遥测…
              </TableCell>
            </TableRow>
          )}
        </TableBody>
      </Table>
    </section>
  );
}

export function ConnectionSummary({
  catalog,
  frame,
}: {
  catalog: Catalog | null;
  frame: Frame | null;
}) {
  return (
    <div className="demo-summary" aria-live="polite">
      <ServerCog aria-hidden="true" />
      <div>
        <span>资料包</span>
        <strong>{catalog?.dataset_id ?? '等待资料包'}</strong>
      </div>
      <div>
        <span>遥测帧</span>
        <strong>{frame ? `#${frame.sequence}` : '尚无有效遥测'}</strong>
      </div>
    </div>
  );
}
