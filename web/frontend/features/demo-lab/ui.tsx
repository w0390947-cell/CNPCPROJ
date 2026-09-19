'use client';

import { Info, LoaderCircle, TriangleAlert } from 'lucide-react';
import { useRouter } from 'next/navigation';
import { useEffect, useState } from 'react';

import { demoClient } from '@/shared/api/demo-client';
import type {
  Catalog,
  FaultRequest,
  Frame,
  ResourceAvailability,
} from '@/shared/api/generated/demo';
import {
  PlatformHeader,
  PlatformNavigation,
} from '@/shared/ui/platform-chrome';

import { AlgorithmPanel, type DemoJob } from './algorithm-panel';
import { CommandPanel } from './command-panel';
import { FaultPanel } from './fault-panel';
import {
  ConnectionSummary,
  DeviceTelemetryPanel,
  NetworkFeedbackPanel,
} from './monitoring-panels';
import { algorithmDefinitions } from './presentation';
import { ResourceAvailabilityPanel } from './resource-availability-panel';

type Notice = { kind: 'error' | 'success'; text: string };

const errorText = (error: unknown) =>
  error instanceof Error ? error.message : String(error);

export function DemoLab() {
  const router = useRouter();
  const [catalog, setCatalog] = useState<Catalog | null>(null);
  const [availability, setAvailability] = useState<ResourceAvailability | null>(
    null,
  );
  const [availabilityError, setAvailabilityError] = useState('');
  const [frame, setFrame] = useState<Frame | null>(null);
  const [connectionError, setConnectionError] = useState('');
  const [notice, setNotice] = useState<Notice | null>(null);
  const [busy, setBusy] = useState(false);
  const [device, setDevice] = useState('SC-storage');
  const [p, setP] = useState('0.5');
  const [q, setQ] = useState('0');
  const [job, setJob] = useState<DemoJob | null>(null);

  useEffect(() => {
    let active = true;
    let timer: ReturnType<typeof setTimeout>;
    let catalogLoaded = false;

    async function poll() {
      try {
        if (!catalogLoaded) {
          const value = await demoClient.catalog();
          if (active) {
            setCatalog(value);
            setDevice((current) =>
              value.devices.some((item) => item.device_id === current)
                ? current
                : (value.devices[0]?.device_id ?? ''),
            );
          }
          catalogLoaded = true;
        }
        const value = await demoClient.telemetry();
        if (active) {
          setFrame(value);
          setConnectionError('');
        }
      } catch (error) {
        if (active) {
          setConnectionError(errorText(error));
          setFrame(null);
        }
      }
      if (active) timer = setTimeout(poll, 1000);
    }

    void poll();
    return () => {
      active = false;
      clearTimeout(timer);
    };
  }, []);

  useEffect(() => {
    let active = true;
    let retryTimer: ReturnType<typeof setTimeout>;

    async function loadAvailability() {
      try {
        const value = await demoClient.availability();
        if (active) {
          setAvailability(value);
          setAvailabilityError('');
        }
      } catch (error) {
        if (active) {
          setAvailabilityError(errorText(error));
          retryTimer = setTimeout(loadAvailability, 10000);
        }
      }
    }

    void loadAvailability();
    return () => {
      active = false;
      clearTimeout(retryTimer);
    };
  }, []);

  async function action(work: () => Promise<void>) {
    setBusy(true);
    setNotice(null);
    try {
      await work();
    } catch (error) {
      setNotice({ kind: 'error', text: errorText(error) });
    } finally {
      setBusy(false);
    }
  }

  const connectionStatus = connectionError
    ? 'offline'
    : frame && catalog
      ? 'online'
      : 'checking';
  const simulatedTime = frame?.simulated_at.slice(11, 16) ?? '--:--';

  return (
    <main className="control-room demo-control-room">
      <a className="skip-link" href="#demo-workspace">
        跳到设备演示工作区
      </a>
      <PlatformHeader
        status={connectionStatus}
        statusLabel={
          connectionStatus === 'online'
            ? '设备服务在线'
            : connectionStatus === 'offline'
              ? '设备服务异常'
              : '正在连接设备服务'
        }
        timeLabel={simulatedTime}
      />
      <PlatformNavigation
        activeItem="demo_lab"
        onNavigate={(item) => router.push(item.href)}
      />

      <section
        aria-busy={busy}
        className="workspace demo-workspace"
        id="demo-workspace"
        tabIndex={-1}
      >
        <div className="section-heading demo-section-heading">
          <h2>模拟设备与算法演示</h2>
          <ConnectionSummary catalog={catalog} frame={frame} />
        </div>

        <div
          className={`context-strip ${connectionError ? 'service-offline' : ''}`}
        >
          <Info aria-hidden="true" size={17} />
          <span className="context-badge">参数化模拟</span>
          <span>设备持续运行 · 每个服务周期推进一分钟模拟时间</span>
          <span className="result-origin">
            {frame
              ? `${frame.simulated_at} · ${frame.quality_valid ? '量测质量有效' : '量测质量无效'}`
              : '尚无有效遥测'}
          </span>
        </div>

        {connectionError && (
          <div className="error-message" role="alert">
            <TriangleAlert aria-hidden="true" size={18} />
            <span>连接或数据异常，无法确认当前安全。{connectionError}</span>
          </div>
        )}
        {notice?.kind === 'error' && (
          <div className="demo-notice error" role="alert">
            <TriangleAlert aria-hidden="true" />
            <span>{notice.text}</span>
          </div>
        )}
        {notice?.kind === 'success' && (
          <output className="demo-notice success">
            <Info aria-hidden="true" />
            <span>{notice.text}</span>
          </output>
        )}
        {busy && (
          <output className="demo-busy">
            <LoaderCircle aria-hidden="true" className="spin" />
            正在处理模拟操作…
          </output>
        )}

        <div className="demo-dashboard-grid">
          <AlgorithmPanel
            busy={busy}
            job={job}
            onRun={(key, request) =>
              void action(async () => {
                const result = await demoClient.run(request);
                const definition = algorithmDefinitions[key];
                const name = definition?.label ?? key;
                setJob({
                  url: `${definition?.route ?? '/'}?simulation=${result.simulation_id}`,
                  name,
                });
                setNotice({ kind: 'success', text: `已提交${name}任务。` });
              })
            }
            presets={catalog?.presets}
          />
          <NetworkFeedbackPanel frame={frame} />
          <ResourceAvailabilityPanel
            availability={availability}
            availabilityError={availabilityError}
            catalog={catalog}
            deviceId={device}
            frame={frame}
            pDraft={p}
            qDraft={q}
          />
          <CommandPanel
            busy={busy}
            catalog={catalog}
            device={device}
            frame={frame}
            onDeviceChange={setDevice}
            onPChange={setP}
            onQChange={setQ}
            onSubmit={(event) => {
              event.preventDefault();
              if (!frame) return;
              void action(async () => {
                const receipt = await demoClient.command({
                  command_id: crypto.randomUUID(),
                  epoch: frame.epoch,
                  device_id: device,
                  p_mw: Number(p),
                  q_mvar: Number(q),
                  expires_at: new Date(
                    Date.parse(frame.simulated_at) + 600000,
                  ).toISOString(),
                });
                setNotice({
                  kind: receipt.status === 'rejected' ? 'error' : 'success',
                  text: `${receipt.status} ${receipt.reason}`.trim(),
                });
              });
            }}
            p={p}
            q={q}
          />
          <DeviceTelemetryPanel frame={frame} />
          <FaultPanel
            busy={busy}
            frame={frame}
            onFault={(fault: FaultRequest['fault']) =>
              void action(async () => {
                setFrame(await demoClient.fault({ fault }));
                setNotice({ kind: 'success', text: '异常场景已更新。' });
              })
            }
            onRearm={() =>
              void action(async () => {
                const result = await demoClient.rearm();
                setNotice({
                  kind: result.rearmed ? 'success' : 'error',
                  text: result.rearmed
                    ? '已解除恢复闭锁，设备保持当前出力；可另行下发命令。'
                    : '未解除：须消除异常并取得至少两个连续安全周期。',
                });
              })
            }
          />
        </div>
      </section>
    </main>
  );
}
