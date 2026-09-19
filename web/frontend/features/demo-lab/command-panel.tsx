import { Send } from 'lucide-react';
import type { ComponentProps } from 'react';

import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select';
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

import { PanelHeading } from './panel-heading';
import { deviceKindLabels } from './presentation';

export function CommandPanel({
  busy,
  catalog,
  device,
  frame,
  p,
  q,
  onDeviceChange,
  onPChange,
  onQChange,
  onSubmit,
}: {
  busy: boolean;
  catalog: Catalog | null;
  device: string;
  frame: Frame | null;
  p: string;
  q: string;
  onDeviceChange: (value: string) => void;
  onPChange: (value: string) => void;
  onQChange: (value: string) => void;
  onSubmit: NonNullable<ComponentProps<'form'>['onSubmit']>;
}) {
  const selectedDevice = catalog?.devices.find(
    (item) => item.device_id === device,
  );

  return (
    <section
      aria-labelledby="commands"
      className="panel demo-panel demo-command-panel"
    >
      <PanelHeading
        headingId="commands"
        icon={Send}
        title="下发模拟 P/Q 命令"
      />
      <p className="demo-panel-copy">
        P、Q 正值表示向网络注入；储能充电为负 P。命令有效期为十个模拟分钟。
      </p>
      <div className="demo-device-select">
        <label htmlFor="demo-command-device">选择设备</label>
        <Select
          disabled={!catalog?.devices.length}
          onValueChange={(value) => value && onDeviceChange(value)}
          value={device}
        >
          <SelectTrigger id="demo-command-device">
            <SelectValue placeholder="等待设备目录">
              {selectedDevice &&
                `${selectedDevice.device_id} · ${deviceKindLabels[selectedDevice.kind]}`}
            </SelectValue>
          </SelectTrigger>
          <SelectContent>
            {catalog?.devices.map((item) => (
              <SelectItem key={item.device_id} value={item.device_id}>
                {item.device_id} · {deviceKindLabels[item.kind]}
              </SelectItem>
            ))}
          </SelectContent>
        </Select>
      </div>
      <form className="demo-command-form" onSubmit={onSubmit}>
        <label htmlFor="demo-command-p">P / MW</label>
        <Input
          id="demo-command-p"
          inputMode="decimal"
          onChange={(event) => onPChange(event.target.value)}
          required
          step="any"
          type="number"
          value={p}
        />
        <label htmlFor="demo-command-q">Q / Mvar</label>
        <Input
          id="demo-command-q"
          inputMode="decimal"
          onChange={(event) => onQChange(event.target.value)}
          required
          step="any"
          type="number"
          value={q}
        />
        <Button disabled={busy || !frame || !device} type="submit">
          <Send aria-hidden="true" />
          发送至 {device || '未选择设备'}
        </Button>
      </form>
      <div className="demo-receipts">
        <h4 id="demo-command-receipts-heading">命令回执</h4>
        <section
          aria-labelledby="demo-command-receipts-heading"
          className="demo-receipts-viewport"
        >
          <Table>
            <TableCaption className="sr-only">
              模拟设备命令的执行回执
            </TableCaption>
            <TableHeader>
              <TableRow>
                <TableHead>命令</TableHead>
                <TableHead>设备</TableHead>
                <TableHead>执行状态</TableHead>
                <TableHead>原因</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {frame?.receipts
                .slice()
                .reverse()
                .map((receipt) => (
                  <TableRow key={receipt.command.command_id}>
                    <TableCell>
                      {receipt.command.command_id.slice(0, 8)}
                    </TableCell>
                    <TableCell>{receipt.command.device_id}</TableCell>
                    <TableCell>{receipt.status}</TableCell>
                    <TableCell>{receipt.reason || '—'}</TableCell>
                  </TableRow>
                ))}
              {!frame?.receipts.length && (
                <TableRow>
                  <TableCell className="demo-table-empty" colSpan={4}>
                    暂无命令回执
                  </TableCell>
                </TableRow>
              )}
            </TableBody>
          </Table>
        </section>
      </div>
    </section>
  );
}
