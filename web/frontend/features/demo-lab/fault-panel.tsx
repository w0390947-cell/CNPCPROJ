import { Activity, RotateCcw } from 'lucide-react';

import { Button } from '@/components/ui/button';
import type { FaultRequest, Frame } from '@/shared/api/generated/demo';

import { PanelHeading } from './panel-heading';

const faults: Array<[FaultRequest['fault'], string]> = [
  ['normal', '正常通信'],
  ['communication_loss', '通信中断'],
  ['bad_quality', '坏质量遥测'],
  ['voltage_sag', '电压跌落'],
  ['ack_timeout', '回执丢失'],
  ['load_drop', '负荷骤降'],
  ['wind_trip', '风机脱网'],
  ['nonconvergence', '潮流不收敛'],
];

export function FaultPanel({
  busy,
  frame,
  onFault,
  onRearm,
}: {
  busy: boolean;
  frame: Frame | null;
  onFault: (fault: FaultRequest['fault']) => void;
  onRearm: () => void;
}) {
  return (
    <section
      aria-labelledby="faults"
      className="panel demo-panel demo-fault-panel"
    >
      <PanelHeading
        headingId="faults"
        icon={Activity}
        title="异常与恢复演示"
        trailing={
          <span className="demo-safe-cycles">
            连续安全周期 <strong>{frame?.safe_cycles ?? '未知'}</strong>
          </span>
        }
      />
      <p className="demo-panel-copy">
        回执丢失时，超时不能证明设备未执行。解除闭锁前必须消除异常并取得连续安全周期。
      </p>
      <div className="demo-actions demo-fault-actions">
        {faults.map(([fault, label]) => (
          <Button
            aria-pressed={frame?.fault === fault}
            disabled={busy || !frame}
            key={fault}
            onClick={() => onFault(fault)}
            variant={frame?.fault === fault ? 'default' : 'outline'}
          >
            {label}
          </Button>
        ))}
        <Button
          className="demo-rearm-button"
          disabled={busy || !frame}
          onClick={onRearm}
          variant="outline"
        >
          <RotateCcw aria-hidden="true" />
          请求解除恢复闭锁
        </Button>
      </div>
    </section>
  );
}
