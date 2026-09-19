'use client';

import {
  Activity,
  AlertTriangle,
  BatteryCharging,
  ChevronRight,
  CircleGauge,
  Download,
  Factory,
  Info,
  FastForward,
  FlaskConical,
  LoaderCircle,
  Network,
  Pause,
  Play,
  RotateCcw,
  ShieldCheck,
  Settings2,
  Square,
  SunMedium,
  Wind,
  Zap,
  RefreshCw,
} from 'lucide-react';
import {
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
  useSyncExternalStore,
} from 'react';
import { useRouter } from 'next/navigation';
import {
  Area,
  ComposedChart,
  CartesianGrid,
  Line,
  ReferenceLine,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from 'recharts';

import { Button } from '@/components/ui/button';
import { Label } from '@/components/ui/label';
import { Progress } from '@/components/ui/progress';
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select';
import {
  Sheet,
  SheetClose,
  SheetContent,
  SheetDescription,
  SheetFooter,
  SheetHeader,
  SheetTitle,
  SheetTrigger,
} from '@/components/ui/sheet';
import { Slider } from '@/components/ui/slider';
import { Switch } from '@/components/ui/switch';
import {
  RenewableEventControls,
  type RenewableSurgeKind,
} from '@/features/simulation-events';
import {
  cancelSimulation,
  createSimulation,
  SimulationCancelledError,
  getHealth,
  getSimulation,
  getSimulationResult,
  waitForSimulation,
  type JobStatus,
  type ScenarioEvent,
  type ScenarioType,
  type SimulationConfiguration,
  type SimulationResult,
} from '@/lib/simulation-api';
import {
  actionLabels,
  clockLabel,
  emptyFrame,
  groupScenarioLabels,
  minuteFrames,
  numberLabel,
  outageAtIteration,
  profileFrames,
  resultConfiguration,
  stateLabels,
  type ProfileFrame,
} from '@/lib/simulation-presentation';
import {
  PlatformHeader,
  PlatformNavigation,
} from '@/shared/ui/platform-chrome';
import {
  REGION_DEFINITIONS,
  regionDisplayName,
} from '@/shared/lib/region-presentation';

type ViewKey = 'overview' | ScenarioType;
type CompletedSimulation = { simulationId: string; result: SimulationResult };
const subscribeHydration = () => () => {};
const clientMounted = () => true;
const serverMounted = () => false;
const eventIdentity = () => crypto.randomUUID();
type EventBranchKind = 'outage' | 'packet_loss' | RenewableSurgeKind;

const renewableSurgeLabels: Record<RenewableSurgeKind, string> = {
  pv_surge: '光伏大发',
  wind_surge: '风电大发',
};
const renewableSurgeMagnitude = 1.35;
const renewableSurgeDurationMinutes = 180;
// SPA 内只记住结果引用；刷新由 URL 中的 simulation ID 恢复，不缓存或伪造结果。
const knownSimulationIds: Partial<Record<ScenarioType, string>> = {};

const regions = REGION_DEFINITIONS;

const viewConfig: Record<
  ViewKey,
  { title: string; scenario: ScenarioType; action: string }
> = {
  overview: {
    title: '三区域协同运行态势',
    scenario: 'cluster_coordination',
    action: '运行集群快照',
  },
  single_microgrid: {
    title: '单微网源网荷储优化',
    scenario: 'single_microgrid',
    action: '运行单微网仿真',
  },
  cluster_coordination: {
    title: '三区域分布式协调',
    scenario: 'cluster_coordination',
    action: '运行集群协调',
  },
  communication_fault: {
    title: '通信失联与自治恢复',
    scenario: 'communication_fault',
    action: '运行故障仿真',
  },
  group_control: {
    title: '光伏群调群控状态机',
    scenario: 'group_control',
    action: '运行群控场景',
  },
};

const defaultConfiguration: SimulationConfiguration = {
  region: 'SC',
  steps: 8,
  storageEnabled: true,
  timeLimitSeconds: 60,
  admmMaxIterations: 220,
  communicationLossProbability: 0.05,
  communicationMaxDelayIterations: 2,
  communicationOutageRegion: 'YA_B',
  communicationOutageStartIteration: 12,
  communicationOutageEndIteration: 22,
};

function MetricCard({
  label,
  value,
  unit,
  note,
  icon: Icon,
  accent,
}: {
  label: string;
  value: string;
  unit: string;
  note: string;
  icon: typeof Zap;
  accent: string;
}) {
  return (
    <section
      className="metric-card"
      style={{ '--accent': accent } as React.CSSProperties}
    >
      <div className="metric-topline">
        <span>{label}</span>
        <Icon aria-hidden="true" size={18} />
      </div>
      <div className="metric-value">
        {value}
        <small>{unit}</small>
      </div>
      <div className="metric-note">
        <span />
        {note}
      </div>
    </section>
  );
}

function RegionNode({
  region,
  index,
  importMw,
  status = '等待仿真',
}: {
  region: (typeof regions)[number];
  index: number;
  importMw?: number;
  status?: string;
}) {
  const faulted = status === '失联' || status === '自治降级';
  return (
    <article
      className={`region-node region-${index + 1} ${faulted ? 'faulted' : ''}`}
    >
      <div className="region-orbit" aria-hidden="true" />
      <div className="region-head">
        <div>
          <b>{region.displayName}</b>
        </div>
        <span className={faulted ? 'fault-dot' : 'node-status'}>{status}</span>
      </div>
      <div className="region-flow">
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
          PCC 计划<strong>{numberLabel(importMw)} MW</strong>
        </span>
      </div>
      <footer>
        {faulted ? <Activity size={14} /> : <Network size={14} />}
        {faulted ? '通信故障回放' : '参数化五节点模型'}
      </footer>
    </article>
  );
}

function SingleMicrogridCanvas({
  current,
  region,
}: {
  current: ProfileFrame;
  region: string;
}) {
  return (
    <div
      className="single-network"
      aria-label={`${regionDisplayName(region)}五节点示意`}
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
      <div className="single-device single-wind">
        <Wind />
        <span>风电计划</span>
        <b>{numberLabel(current.wind)} MW</b>
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

type GroupRecord = NonNullable<
  SimulationResult['group_control']
>['records'][number];

function GroupControlCanvas({ record }: { record?: GroupRecord }) {
  const states = [
    ['normal', '正常运行'],
    ['prepared', '调控准备'],
    ['risk_observing', '风险观察'],
    ['curtailing', '执行限发'],
    ['curtailed_hold', '限发保持'],
    ['restore_wait', '恢复等待'],
    ['restoring', '恢复监测'],
    ['recovery_inhibit', '恢复闭锁'],
    ['recovery_aborted', '恢复中止'],
    ['output_block', '停止新写入'],
    ['hard_override', '硬保护接管'],
  ];
  return (
    <div className="group-state-machine">
      <div className="threshold-readout">
        <span>
          PCC 实绩<strong>{record?.pcc_power_mw.toFixed(2) ?? '—'} MW</strong>
        </span>
        <span>
          风险限值<strong>{record?.risk_limit_mw.toFixed(2) ?? '—'} MW</strong>
        </span>
        <span>
          安全阈值
          <strong>{record?.safety_threshold_mw.toFixed(2) ?? '—'} MW</strong>
        </span>
        <span>
          恢复阈值
          <strong>{record?.restore_threshold_mw.toFixed(2) ?? '—'} MW</strong>
        </span>
      </div>
      <div className="state-flow">
        {states.map(([id, label], index) => (
          <div key={id} className="state-step-wrap">
            <div
              className={`state-step ${record?.state === id || (id === 'restoring' && record?.state === 'restoring_dwell') ? 'active' : ''}`}
            >
              <small>0{index + 1}</small>
              <b>{label}</b>
            </div>
          </div>
        ))}
      </div>
      <div className="control-decision">
        <span>当前控制决策</span>
        <b>
          {record
            ? `${stateLabels[record.state] ?? record.state} · ${actionLabels[record.action] ?? record.action}`
            : '运行后查看状态；上方为可达状态，不代表固定执行顺序'}
        </b>
        {record && <code>{record.trigger_reason || '本周期无新动作'}</code>}
      </div>
    </div>
  );
}

export default function SimulationDashboard({
  initialView = 'overview',
}: {
  initialView?: ViewKey;
}) {
  const router = useRouter();
  const mounted = useSyncExternalStore(
    subscribeHydration,
    clientMounted,
    serverMounted,
  );
  const [serviceStatus, setServiceStatus] = useState<
    'checking' | 'online' | 'offline'
  >('checking');
  const selectedView = initialView;
  const [isPlaying, setIsPlaying] = useState(false);
  const [cursor, setCursor] = useState(0);
  const [speed, setSpeed] = useState(1);
  const [configuration, setConfiguration] =
    useState<SimulationConfiguration>(defaultConfiguration);
  const [completedSimulations, setCompletedSimulations] = useState<
    Partial<Record<ScenarioType, CompletedSimulation>>
  >({});
  const [branchBaselines, setBranchBaselines] = useState<
    Partial<Record<ScenarioType, SimulationResult>>
  >({});
  const [runState, setRunState] = useState<
    'ready' | 'running' | 'success' | 'error' | 'cancelled'
  >('ready');
  const [runMessage, setRunMessage] = useState('');
  const [lastEventLabel, setLastEventLabel] = useState('');
  const [eventMarkerPercent, setEventMarkerPercent] = useState<number | null>(
    null,
  );
  const [jobStatus, setJobStatus] = useState<JobStatus | null>(null);
  const [isCancelling, setIsCancelling] = useState(false);
  const activeSimulationId = useRef<string | null>(null);
  const hydratedSimulationId = useRef<string | null>(null);
  const runLock = useRef(false);
  const subscription = useRef<AbortController | null>(null);
  const [groupScenario, setGroupScenario] = useState(
    'successful_progressive_recovery',
  );
  const [validationFilter, setValidationFilter] = useState<'all' | 'failed'>(
    'all',
  );
  const [serviceChecking, setServiceChecking] = useState(false);
  const activeView = viewConfig[selectedView];
  const completedSimulation = completedSimulations[activeView.scenario];
  const simulation = completedSimulation?.result ?? null;
  const isIterationView =
    selectedView === 'communication_fault' ||
    selectedView === 'cluster_coordination';
  const resultRegion = simulation?.request.region ?? configuration.region;
  const groupScenarios = Object.keys(
    simulation?.group_control?.scenario_checks ?? {},
  );
  const effectiveGroupScenario = groupScenarios.includes(groupScenario)
    ? groupScenario
    : groupScenarios[0];
  const selectedGroupRecords = useMemo(
    () =>
      simulation?.group_control?.records.filter(
        (record) => record.scenario === effectiveGroupScenario,
      ) ?? [],
    [simulation, effectiveGroupScenario],
  );

  async function recheckService() {
    setServiceChecking(true);
    try {
      setServiceStatus(
        (await getHealth()).status === 'ok' ? 'online' : 'offline',
      );
    } catch {
      setServiceStatus('offline');
    } finally {
      setServiceChecking(false);
    }
  }

  function exportResult() {
    if (!completedSimulation) return;
    const blob = new Blob(
      [JSON.stringify(completedSimulation.result, null, 2)],
      { type: 'application/json' },
    );
    const url = URL.createObjectURL(blob);
    const anchor = document.createElement('a');
    anchor.href = url;
    anchor.download = `oilfield-${activeView.scenario}-${completedSimulation.simulationId}.json`;
    anchor.click();
    window.setTimeout(() => URL.revokeObjectURL(url), 1000);
  }

  function updateConfiguration<K extends keyof SimulationConfiguration>(
    key: K,
    value: SimulationConfiguration[K],
  ) {
    setConfiguration((currentConfiguration) => ({
      ...currentConfiguration,
      [key]: value,
    }));
  }

  const chartData = useMemo(
    () => profileFrames(simulation, selectedView === 'single_microgrid'),
    [simulation, selectedView],
  );
  const playbackData = useMemo(() => {
    if (selectedView === 'group_control')
      return selectedGroupRecords.map((r) => ({
        ...emptyFrame,
        minute: r.time_minute,
        time: clockLabel(r.time_minute),
        import: r.pcc_power_mw,
      }));
    if (isIterationView)
      return (
        simulation?.admm_history.map((h) => ({
          ...emptyFrame,
          minute: h.iteration,
          time: `第 ${h.iteration} 轮`,
        })) ?? []
      );
    return minuteFrames(chartData);
  }, [
    chartData,
    simulation,
    selectedView,
    isIterationView,
    selectedGroupRecords,
  ]);

  const runQuick = useCallback(
    async (events: ScenarioEvent[] = [], runConfiguration = configuration) => {
      if (runLock.current || activeSimulationId.current) return null;
      runLock.current = true;
      const controller = new AbortController();
      subscription.current = controller;
      setIsPlaying(false);
      setRunState('running');
      const eventLabel = events.at(-1)?.label;
      setRunMessage(
        `正在创建独立仿真任务…${eventLabel ? ` · ${eventLabel}` : ''}`,
      );
      setJobStatus(null);
      if (!events.length) {
        setLastEventLabel('');
        setEventMarkerPercent(null);
        setBranchBaselines((existing) => {
          const next = { ...existing };
          delete next[activeView.scenario];
          return next;
        });
      }
      try {
        const scenario = activeView.scenario;
        const created = await createSimulation(
          scenario,
          runConfiguration,
          events,
        );
        controller.signal.throwIfAborted();
        activeSimulationId.current = created.simulation_id;
        const sessionUrl = new URL(window.location.href);
        sessionUrl.searchParams.set('simulation', created.simulation_id);
        window.history.replaceState(null, '', sessionUrl);
        const result = await waitForSimulation(
          created,
          (status) => {
            setJobStatus(status);
            setRunMessage(
              `${status.stage_label ?? '正在运行仿真任务…'}${eventLabel ? ` · ${eventLabel}` : ''}`,
            );
          },
          350,
          controller.signal,
        );
        controller.signal.throwIfAborted();
        setCompletedSimulations((existing) => ({
          ...existing,
          [scenario]: { simulationId: created.simulation_id, result },
        }));
        knownSimulationIds[scenario] = created.simulation_id;
        setCursor(0);
        setRunState('success');
        setRunMessage(
          events.length
            ? `${result.executive_summary.headline} · ${events.at(-1)?.label}`
            : result.executive_summary.headline,
        );
        setLastEventLabel(events.at(-1)?.label ?? '');
        return result;
      } catch (error) {
        if (controller.signal.aborted) return null;
        if (error instanceof SimulationCancelledError) {
          setRunState('cancelled');
          setRunMessage('仿真已安全取消');
        } else {
          setRunState('error');
          setRunMessage(
            error instanceof Error ? error.message : '快速仿真失败',
          );
        }
        return null;
      } finally {
        runLock.current = false;
        activeSimulationId.current = null;
        setIsCancelling(false);
      }
    },
    [activeView.scenario, configuration],
  );

  async function cancelRun() {
    const simulationId = activeSimulationId.current;
    if (!simulationId || isCancelling) return;
    setIsCancelling(true);
    setRunMessage('正在取消独立仿真进程…');
    try {
      const status = await cancelSimulation(simulationId);
      setJobStatus(status);
    } catch (error) {
      setRunMessage(error instanceof Error ? error.message : '取消仿真失败');
      setIsCancelling(false);
    }
  }

  async function runEventBranch(
    kind: EventBranchKind,
    renewableTarget?: SimulationConfiguration['region'],
  ) {
    if (
      runLock.current ||
      activeSimulationId.current ||
      serviceStatus !== 'online' ||
      !playbackData.length
    )
      return;
    setIsPlaying(false);
    const injectionTime = current.time;
    const injectionCursor = cursor;
    const baseline = simulation;
    if (!baseline) return;
    if (
      (kind === 'pv_surge' || kind === 'wind_surge') &&
      selectedView === 'overview' &&
      !renewableTarget
    )
      return;
    const branchConfig = resultConfiguration(baseline);
    setBranchBaselines((existing) => ({
      ...existing,
      [activeView.scenario]: baseline,
    }));
    setEventMarkerPercent(
      (injectionCursor / Math.max(1, playbackData.length - 1)) * 100,
    );
    if (kind === 'outage' || kind === 'packet_loss') {
      const start =
        baseline.admm_history[
          Math.min(injectionCursor, baseline.admm_history.length - 1)
        ]?.iteration;
      if (start == null) return;
      await runQuick(
        [
          ...baseline.request.events,
          {
            event_id: eventIdentity(),
            event_type:
              kind === 'packet_loss'
                ? 'communication_packet_loss'
                : 'communication_outage',
            target: branchConfig.communicationOutageRegion,
            time_axis: 'coordination_iteration',
            start,
            end: start + 8,
            magnitude: kind === 'packet_loss' ? 0.35 : 1,
            label:
              kind === 'packet_loss'
                ? `第 ${start}—${start + 8} 轮 35% 丢包`
                : `${regionDisplayName(branchConfig.communicationOutageRegion)}第 ${start}—${start + 8} 轮失联`,
          },
        ],
        branchConfig,
      );
      return;
    }
    const [hours, minutes] = injectionTime.split(':').map(Number);
    const start = hours * 60 + minutes;
    const surgeLabel = renewableSurgeLabels[kind];
    // Event scope is an explicit action; every other parameter stays with the baseline.
    const target =
      selectedView === 'overview' && renewableTarget
        ? renewableTarget
        : branchConfig.region;
    await runQuick(
      [
        ...baseline.request.events,
        {
          event_id: eventIdentity(),
          event_type: kind,
          target,
          time_axis: 'clock_minute',
          start,
          end: Math.min(1440, start + renewableSurgeDurationMinutes),
          magnitude: renewableSurgeMagnitude,
          label: `${regionDisplayName(target)}${injectionTime} 起${surgeLabel}`,
        },
      ],
      branchConfig,
    );
  }

  useEffect(() => {
    let alive = true;
    const check = () =>
      getHealth().then(
        (health) => {
          if (alive)
            setServiceStatus(health.status === 'ok' ? 'online' : 'offline');
        },
        () => {
          if (alive) setServiceStatus('offline');
        },
      );
    void check();
    const interval = window.setInterval(check, 15000);
    return () => {
      alive = false;
      window.clearInterval(interval);
      subscription.current?.abort();
    };
  }, []);

  useEffect(() => {
    const simulationId = new URL(window.location.href).searchParams.get(
      'simulation',
    );
    if (!simulationId || hydratedSimulationId.current === simulationId) return;
    hydratedSimulationId.current = simulationId;
    const scenario = viewConfig[initialView].scenario;
    let alive = true;
    const controller = new AbortController();
    subscription.current = controller;

    async function hydrateSession() {
      setRunState('running');
      setRunMessage('正在恢复仿真会话…');
      activeSimulationId.current = simulationId;
      try {
        const status = await getSimulation(simulationId!);
        if (!alive) return;
        setJobStatus(status);
        const result =
          status.state === 'succeeded'
            ? await getSimulationResult(simulationId!)
            : await waitForSimulation(
                status,
                (update) => {
                  if (!alive) return;
                  setJobStatus(update);
                  setRunMessage(update.stage_label ?? '正在恢复仿真会话…');
                },
                350,
                controller.signal,
              );
        if (!alive) return;
        if (result.metadata.scenario_type !== scenario)
          throw new Error('此任务不属于当前场景，请使用原场景页面打开。');
        setCompletedSimulations((existing) => ({
          ...existing,
          [scenario]: { simulationId: simulationId!, result },
        }));
        knownSimulationIds[scenario] = simulationId!;
        setRunState('success');
        setRunMessage(result.executive_summary.headline);
      } catch (error) {
        if (!alive) return;
        setRunState(
          error instanceof SimulationCancelledError ? 'cancelled' : 'error',
        );
        setRunMessage(
          error instanceof Error ? error.message : '恢复仿真会话失败',
        );
      } finally {
        if (activeSimulationId.current === simulationId)
          activeSimulationId.current = null;
      }
    }

    void hydrateSession();
    return () => {
      alive = false;
      controller.abort();
    };
  }, [initialView]);

  useEffect(() => {
    if (
      !isPlaying ||
      playbackData.length < 2 ||
      cursor >= playbackData.length - 1
    )
      return;
    const timer = window.setInterval(
      () => setCursor((value) => Math.min(value + 1, playbackData.length - 1)),
      1000 / speed,
    );
    return () => window.clearInterval(timer);
  }, [isPlaying, playbackData.length, speed, cursor]);

  useEffect(() => {
    type ToolContext = {
      registerTool?: (
        tool: object,
        options?: { signal?: AbortSignal },
      ) => void | Promise<void>;
    };
    const context = (document as Document & { modelContext?: ToolContext })
      .modelContext;
    if (!context?.registerTool) return;
    const lifecycle = new AbortController();
    void Promise.resolve(
      context.registerTool(
        {
          name: 'run_quick_simulation',
          title: '运行当前场景仿真',
          description: `真实运行“${activeView.title}”场景，并将求解结果更新到当前驾驶舱。`,
          inputSchema: {
            type: 'object',
            properties: {},
            additionalProperties: false,
          },
          annotations: { readOnlyHint: false, untrustedContentHint: false },
          execute: async () => {
            const result = await runQuick();
            if (!result) return { completed: false };
            return {
              completed: true,
              passed: result.executive_summary.overall_passed,
              improvement_percent:
                result.executive_summary.economic_improvement_percent,
              points: result.timeseries.length,
            };
          },
        },
        { signal: lifecycle.signal },
      ),
    ).catch(() => undefined);
    return () => lifecycle.abort();
  }, [activeView.title, runQuick]);

  const current =
    playbackData[Math.min(cursor, playbackData.length - 1)] ?? emptyFrame;
  const playbackActive = isPlaying && cursor < playbackData.length - 1;
  const clusterPoint = !isIterationView
    ? simulation?.cluster_timeseries.findLast(
        (p) => p.time_hour * 60 <= current.minute,
      )
    : null;
  const iterationRecord = isIterationView
    ? simulation?.admm_history[Math.min(cursor, playbackData.length - 1)]
    : undefined;
  const currentGroupRecord = selectedGroupRecords.length
    ? selectedGroupRecords[Math.min(cursor, selectedGroupRecords.length - 1)]
    : undefined;
  const improvement =
    simulation?.executive_summary.economic_improvement_percent;
  const overallPassed = simulation?.executive_summary.overall_passed ?? null;
  const jobProgress =
    jobStatus?.stage_sequence == null
      ? 0
      : Math.round((jobStatus.stage_sequence / jobStatus.total_stages) * 100);
  const communication = simulation?.communication;
  const groupControl = simulation?.group_control;
  const scenarioMetrics = useMemo(() => {
    if (selectedView === 'group_control') {
      const actions = groupControl?.records.filter(
        (record) => record.action !== 'none',
      ).length;
      return [
        {
          label: '回归场景',
          value: numberLabel(groupControl?.scenario_count, 0),
          unit: '类',
          note: '覆盖触发、恢复与故障',
          icon: CircleGauge,
          accent: '#36a9ff',
        },
        {
          label: '通过场景',
          value: groupControl
            ? String(
                Object.values(groupControl.scenario_checks).filter((checks) =>
                  Object.values(checks).every(Boolean),
                ).length,
              )
            : '—',
          unit: '类',
          note:
            groupControl?.all_passed === false
              ? '存在未通过检查'
              : groupControl
                ? '状态机检查通过'
                : '等待运行真实状态机',
          icon: ShieldCheck,
          accent: '#36d7cb',
        },
        {
          label: '控制动作',
          value: numberLabel(actions, 0),
          unit: '次',
          note: '全部场景的非空动作',
          icon: Activity,
          accent: '#f3c95d',
        },
        {
          label: '审计事件',
          value: numberLabel(groupControl?.events.length, 0),
          unit: '条',
          note: '状态转换全程留痕',
          icon: Network,
          accent: '#9d7bff',
        },
      ];
    }
    if (selectedView === 'communication_fault') {
      return [
        {
          label: '通信报文',
          value: numberLabel(communication?.sent, 0),
          unit: '条',
          note: '本次仿真累计值',
          icon: Network,
          accent: '#36a9ff',
        },
        {
          label: '丢弃报文',
          value: numberLabel(communication?.dropped, 0),
          unit: '条',
          note: `其中失联丢弃 ${numberLabel(communication?.outage_dropped, 0)} 条`,
          icon: Activity,
          accent: '#ff6d72',
        },
        {
          label: '自治降级',
          value: numberLabel(communication?.fallback_uses, 0),
          unit: '次',
          note: '陈旧超限后本地接管',
          icon: ShieldCheck,
          accent: '#f3c95d',
        },
        {
          label: '当前协调轮次',
          value: numberLabel(iterationRecord?.iteration, 0),
          unit: '轮',
          note: '原生迭代记录 · 非分钟',
          icon: CircleGauge,
          accent: '#36d7cb',
        },
      ];
    }
    if (
      selectedView === 'cluster_coordination' ||
      selectedView === 'overview'
    ) {
      return [
        {
          label: isIterationView ? '计划峰值负荷' : '集群总负荷',
          value: numberLabel(
            isIterationView
              ? simulation?.cluster_timeseries.length
                ? Math.max(
                    ...simulation.cluster_timeseries.map(
                      (p) => p.aggregate_load_mw,
                    ),
                  )
                : null
              : current.load,
          ),
          unit: 'MW',
          note: '三区域参数化计划',
          icon: Factory,
          accent: '#36a9ff',
        },
        {
          label: isIterationView ? '计划峰值新能源' : '新能源出力',
          value: numberLabel(
            isIterationView
              ? simulation?.cluster_timeseries.length
                ? Math.max(
                    ...simulation.cluster_timeseries.map(
                      (p) => p.aggregate_renewable_mw,
                    ),
                  )
                : null
              : current.renewable,
          ),
          unit: 'MW',
          note: '风电与光伏合计',
          icon: SunMedium,
          accent: '#f3c95d',
        },
        {
          label: isIterationView ? '计划峰值受电' : '集群受电',
          value: numberLabel(
            isIterationView
              ? simulation?.cluster_timeseries.length
                ? Math.max(
                    ...simulation.cluster_timeseries.map(
                      (p) => p.aggregate_import_mw,
                    ),
                  )
                : null
              : current.import,
          ),
          unit: 'MW',
          note: '正值表示从上级电网受电',
          icon: Zap,
          accent: '#36d7cb',
        },
        {
          label: '协调记录',
          value: simulation ? String(simulation.admm_history.length) : '—',
          unit: '条',
          note: simulation ? '真实 ADMM 计算结果' : '等待运行仿真',
          icon: Activity,
          accent: '#9d7bff',
        },
      ];
    }
    return [
      {
        label: '微网负荷',
        value: numberLabel(current.load),
        unit: 'MW',
        note: `${resultRegion} 参数化负荷`,
        icon: Factory,
        accent: '#36a9ff',
      },
      {
        label: '新能源出力',
        value: numberLabel(current.renewable),
        unit: 'MW',
        note: '风电与光伏合计',
        icon: SunMedium,
        accent: '#f3c95d',
      },
      {
        label: 'PCC 受电',
        value: numberLabel(current.import),
        unit: 'MW',
        note: '正值受电 · 负值倒送',
        icon: Zap,
        accent: '#36d7cb',
      },
      {
        label: '储能对照收益',
        value: numberLabel(improvement),
        unit: '%',
        note: '相对禁储能对照 · 非现场提升',
        icon: Activity,
        accent: '#9d7bff',
      },
    ];
  }, [
    communication,
    current,
    groupControl,
    improvement,
    isIterationView,
    iterationRecord,
    resultRegion,
    selectedView,
    simulation,
  ]);
  const verdictItems: Array<{
    code: string;
    label: string;
    passed: boolean | null;
    explanation: string;
    scope?: string;
  }> = simulation?.validation_items ?? [
    { code: 'ADMM', label: '三区域协调收敛', passed: null, explanation: '' },
    { code: 'PCC', label: 'PCC 防倒送约束', passed: null, explanation: '' },
    {
      code: 'VOLTAGE',
      label: '节点电压与线路容量',
      passed: null,
      explanation: '',
    },
    {
      code: 'STORAGE',
      label: '储能 SOC 与设备响应',
      passed: null,
      explanation: '',
    },
  ];
  const conclusion =
    selectedView === 'group_control'
      ? {
          label: '群控审计事件',
          value: numberLabel(groupControl?.events.length, 0),
          unit: '条',
        }
      : selectedView === 'communication_fault'
        ? {
            label: '失联期间丢弃报文',
            value: numberLabel(communication?.outage_dropped, 0),
            unit: '条',
          }
        : {
            label: '相对禁储能对照收益',
            value: numberLabel(improvement),
            unit: '%',
          };
  const groupChartData = selectedGroupRecords.map((record) => ({
    time: record.time_minute,
    pcc: record.pcc_power_mw,
    risk: record.risk_limit_mw,
    safety: record.safety_threshold_mw,
    restore: record.restore_threshold_mw,
  }));
  const admmChartData =
    simulation?.admm_history.map((item) => ({
      ...item,
      primal_residual: Math.max(item.primal_residual, 1e-9),
      dual_residual: Math.max(item.dual_residual, 1e-9),
      primal_tolerance: Math.max(item.primal_tolerance, 1e-9),
      dual_tolerance: Math.max(item.dual_tolerance, 1e-9),
    })) ?? [];
  const topologyTitle =
    selectedView === 'single_microgrid'
      ? `${resultRegion} 微电网五节点示意`
      : selectedView === 'group_control'
        ? '光伏群控监督器状态转换'
        : '跨区域微电网集群';
  const capacityLabel =
    selectedView === 'group_control'
      ? '综合风险指数'
      : isIterationView
        ? '当前新鲜区域数 / 总区域数'
        : selectedView === 'single_microgrid'
          ? '储能正值放电 · SVG正值容性'
          : 'PCC 受电 / 本场景通道上限';
  const capacityPercent =
    selectedView === 'group_control'
      ? currentGroupRecord
        ? currentGroupRecord.risk_index * 100
        : null
      : isIterationView
        ? iterationRecord
          ? (iterationRecord.fresh_region_count / 3) * 100
          : null
        : clusterPoint?.import_limit_mw && current.import != null
          ? (current.import / clusterPoint.import_limit_mw) * 100
          : null;
  const validationPassed =
    simulation?.validation_items.filter((i) => i.passed).length ?? 0;
  const outageNow = outageAtIteration(
    simulation,
    iterationRecord?.iteration ?? -1,
  );
  const branchBaseline = branchBaselines[activeView.scenario];
  const branchComparison =
    branchBaseline && simulation && lastEventLabel
      ? selectedView === 'communication_fault'
        ? ([
            [
              '丢弃报文',
              branchBaseline.communication?.dropped ?? 0,
              simulation.communication?.dropped ?? 0,
              '条',
            ],
            [
              '自治降级',
              branchBaseline.communication?.fallback_uses ?? 0,
              simulation.communication?.fallback_uses ?? 0,
              '次',
            ],
            [
              '协调迭代',
              branchBaseline.admm_history.length,
              simulation.admm_history.length,
              '轮',
            ],
          ] as const)
        : ([
            [
              '运行成本',
              branchBaseline.executive_summary.optimized_economic_cost_cny,
              simulation.executive_summary.optimized_economic_cost_cny,
              '元',
            ],
            [
              '峰值受电',
              branchBaseline.executive_summary.optimized_peak_import_mw,
              simulation.executive_summary.optimized_peak_import_mw,
              'MW',
            ],
            [
              '有功网损',
              branchBaseline.executive_summary.total_active_loss_mwh,
              simulation.executive_summary.total_active_loss_mwh,
              'MWh',
            ],
          ] as const)
      : [];

  return (
    <main className={`control-room ${!simulation ? 'awaiting-result' : ''}`}>
      <a className="skip-link" href="#simulation-workspace">
        跳到仿真工作区
      </a>
      <PlatformHeader
        status={serviceStatus}
        statusLabel={
          serviceStatus === 'online'
            ? '求解服务在线'
            : serviceStatus === 'offline'
              ? '求解服务离线'
              : '正在检查服务'
        }
        timeLabel={current.time}
      />

      <PlatformNavigation
        activeItem={selectedView}
        isItemDisabled={(item) =>
          runState === 'running' && item.id !== 'demo_lab'
        }
        onNavigate={(item) => {
          if (item.id === 'demo_lab' || item.id === 'case_information') {
            router.push(item.href);
            return;
          }
          setCursor(0);
          setRunState('ready');
          setJobStatus(null);
          setLastEventLabel('');
          setEventMarkerPercent(null);
          const sessionId =
            completedSimulations[viewConfig[item.id].scenario]?.simulationId ??
            knownSimulationIds[viewConfig[item.id].scenario];
          router.push(
            `${item.href}${sessionId ? `?simulation=${sessionId}` : ''}`,
          );
        }}
      />

      <section className="workspace" id="simulation-workspace" tabIndex={-1}>
        <div className="section-heading">
          <h2>{activeView.title}</h2>
          <div className="run-actions">
            <output className={`run-feedback ${runState}`} aria-live="polite">
              <span>
                {runState === 'ready'
                  ? simulation
                    ? '已载入仿真结果'
                    : '准备运行'
                  : runMessage}
              </span>
              <b>
                {runState === 'running' && jobStatus
                  ? `任务 ${jobStatus.simulation_id.slice(0, 8)} · ${jobProgress}%`
                  : simulation
                    ? `已计算 · ${simulation.metadata.steps} 时段${lastEventLabel ? ' · 事件分支' : ''}`
                    : '选择参数后运行 · 不预填结果'}
              </b>
              {runState === 'running' && (
                <Progress className="job-progress" value={jobProgress} />
              )}
            </output>
            <Sheet>
              <SheetTrigger
                render={
                  <Button
                    className="config-button"
                    disabled={runState === 'running'}
                    variant="outline"
                  />
                }
              >
                <Settings2 />
                仿真参数
              </SheetTrigger>
              <SheetContent className="simulation-config-sheet">
                <SheetHeader>
                  <SheetTitle>仿真运行配置</SheetTitle>
                  <SheetDescription>
                    参数用于下一次仿真；修改参数不会改变已生成的结果。数据口径见“算例与数据说明”。
                  </SheetDescription>
                </SheetHeader>
                <div className="config-body">
                  {selectedView === 'overview' ||
                  selectedView === 'cluster_coordination' ? (
                    <div className="config-field config-scope">
                      <span>协调范围</span>
                      <p>
                        {regions.map((region) => region.displayName).join('、')}
                      </p>
                    </div>
                  ) : (
                    <div className="config-field">
                      <Label htmlFor="region-select">
                        {selectedView === 'single_microgrid'
                          ? '仿真区域'
                          : '重点展示区域'}
                      </Label>
                      <Select
                        value={configuration.region}
                        onValueChange={(value) => {
                          const region = regions.find(
                            (item) => item.id === value,
                          );
                          if (region) updateConfiguration('region', region.id);
                        }}
                      >
                        <SelectTrigger
                          className="config-select"
                          id="region-select"
                        >
                          <SelectValue />
                        </SelectTrigger>
                        <SelectContent>
                          {regions.map((region) => (
                            <SelectItem key={region.id} value={region.id}>
                              {region.displayName}
                            </SelectItem>
                          ))}
                        </SelectContent>
                      </Select>
                    </div>
                  )}
                  <div className="config-field">
                    <Label htmlFor="steps-select">优化时段数</Label>
                    <Select
                      value={String(configuration.steps)}
                      onValueChange={(value) =>
                        value && updateConfiguration('steps', Number(value))
                      }
                    >
                      <SelectTrigger
                        className="config-select"
                        id="steps-select"
                      >
                        <SelectValue />
                      </SelectTrigger>
                      <SelectContent>
                        <SelectItem value="4">4 · 集群极速验证</SelectItem>
                        <SelectItem value="8">8 · 单微网快速演示</SelectItem>
                        <SelectItem value="24">24 · 标准分析</SelectItem>
                        <SelectItem value="96">96 · 15分钟完整计算</SelectItem>
                      </SelectContent>
                    </Select>
                  </div>
                  <div className="config-field switch-field">
                    <div>
                      <Label htmlFor="storage-switch">启用储能优化</Label>
                      <span>参与充放电与 SOC 约束</span>
                    </div>
                    <Switch
                      id="storage-switch"
                      checked={configuration.storageEnabled}
                      onCheckedChange={(checked) =>
                        updateConfiguration('storageEnabled', checked)
                      }
                    />
                  </div>
                  <div className="config-field slider-field">
                    <div>
                      <Label>单次求解时限</Label>
                      <b>{configuration.timeLimitSeconds} 秒</b>
                    </div>
                    <Slider
                      aria-label="单次求解时限，秒"
                      min={30}
                      max={300}
                      step={30}
                      value={[configuration.timeLimitSeconds]}
                      onValueChange={(value) =>
                        updateConfiguration(
                          'timeLimitSeconds',
                          typeof value === 'number' ? value : value[0],
                        )
                      }
                    />
                  </div>

                  {(activeView.scenario === 'cluster_coordination' ||
                    activeView.scenario === 'communication_fault') && (
                    <>
                      <div className="config-section-title">集群协调</div>
                      <div className="config-field slider-field">
                        <div>
                          <Label>ADMM 最大迭代</Label>
                          <b>{configuration.admmMaxIterations} 轮</b>
                        </div>
                        <Slider
                          aria-label="ADMM 最大迭代轮次"
                          min={100}
                          max={600}
                          step={20}
                          value={[configuration.admmMaxIterations]}
                          onValueChange={(value) =>
                            updateConfiguration(
                              'admmMaxIterations',
                              typeof value === 'number' ? value : value[0],
                            )
                          }
                        />
                      </div>
                    </>
                  )}

                  {activeView.scenario === 'communication_fault' && (
                    <>
                      <div className="config-section-title">通信故障</div>
                      <div className="config-field">
                        <Label htmlFor="outage-region-select">失联区域</Label>
                        <Select
                          value={configuration.communicationOutageRegion}
                          onValueChange={(value) =>
                            value &&
                            updateConfiguration(
                              'communicationOutageRegion',
                              value as SimulationConfiguration['communicationOutageRegion'],
                            )
                          }
                        >
                          <SelectTrigger
                            className="config-select"
                            id="outage-region-select"
                          >
                            <SelectValue />
                          </SelectTrigger>
                          <SelectContent>
                            {regions.map((region) => (
                              <SelectItem key={region.id} value={region.id}>
                                {region.displayName}
                              </SelectItem>
                            ))}
                          </SelectContent>
                        </Select>
                      </div>
                      <div className="config-field slider-field">
                        <div>
                          <Label>随机丢包率</Label>
                          <b>
                            {Math.round(
                              configuration.communicationLossProbability * 100,
                            )}
                            %
                          </b>
                        </div>
                        <Slider
                          aria-label="随机丢包率"
                          min={0}
                          max={0.3}
                          step={0.01}
                          value={[configuration.communicationLossProbability]}
                          onValueChange={(value) =>
                            updateConfiguration(
                              'communicationLossProbability',
                              typeof value === 'number' ? value : value[0],
                            )
                          }
                        />
                      </div>
                      <div className="config-field slider-field">
                        <div>
                          <Label>最大通信延迟</Label>
                          <b>
                            {configuration.communicationMaxDelayIterations} 轮
                          </b>
                        </div>
                        <Slider
                          aria-label="最大通信延迟轮次"
                          min={0}
                          max={8}
                          step={1}
                          value={[
                            configuration.communicationMaxDelayIterations,
                          ]}
                          onValueChange={(value) =>
                            updateConfiguration(
                              'communicationMaxDelayIterations',
                              typeof value === 'number' ? value : value[0],
                            )
                          }
                        />
                      </div>
                    </>
                  )}
                </div>
                <SheetFooter>
                  <div className="configuration-summary">
                    <span>当前方案</span>
                    <b>
                      {configuration.steps} 时段 ·{' '}
                      {configuration.timeLimitSeconds} 秒 ·{' '}
                      {configuration.storageEnabled ? '储能启用' : '储能停用'} ·
                      MISOCP
                    </b>
                  </div>
                  <SheetClose
                    render={<Button className="apply-config-button" />}
                  >
                    完成参数设置
                  </SheetClose>
                </SheetFooter>
              </SheetContent>
            </Sheet>
            {selectedView === 'communication_fault' && (
              <Button
                className="inject-button"
                disabled={
                  runState === 'running' ||
                  !simulation ||
                  serviceStatus !== 'online'
                }
                onClick={() => {
                  void runEventBranch('outage');
                }}
                variant="outline"
              >
                <Activity />
                当前轮次注入失联
              </Button>
            )}
            {selectedView === 'communication_fault' && (
              <Button
                className="inject-button"
                disabled={
                  runState === 'running' ||
                  !simulation ||
                  serviceStatus !== 'online'
                }
                onClick={() => {
                  void runEventBranch('packet_loss');
                }}
                variant="outline"
              >
                <Network />
                当前轮次注入丢包
              </Button>
            )}
            <Button
              className={`run-button ${runState === 'running' ? 'cancel-button' : ''}`}
              disabled={
                runState === 'running'
                  ? !jobStatus || isCancelling
                  : serviceStatus !== 'online'
              }
              onClick={
                runState === 'running'
                  ? cancelRun
                  : () => {
                      void runQuick();
                    }
              }
            >
              {runState === 'running' ? (
                isCancelling ? (
                  <LoaderCircle className="spin" />
                ) : (
                  <Square />
                )
              ) : (
                <FlaskConical />
              )}
              {runState === 'running'
                ? isCancelling
                  ? '正在取消'
                  : '取消仿真'
                : activeView.action}
            </Button>
          </div>
        </div>

        <div
          className={`context-strip ${serviceStatus === 'offline' ? 'service-offline' : ''}`}
        >
          <Info size={17} aria-hidden="true" />
          {serviceStatus === 'offline' ? (
            <span>
              本地求解服务未连接。请启动 Python API；已有结果仍可回放。
            </span>
          ) : (
            <>
              <span className="context-badge">参数化模拟</span>
              {simulation?.cluster_validation?.scope === 'reference_only' && (
                <span>校核范围：集中式基准与协调参考；分布式执行未校核</span>
              )}
              {simulation &&
                ['cluster_coordination', 'communication_fault'].includes(
                  simulation.metadata.scenario_type,
                ) &&
                !simulation.cluster_validation && (
                  <span>历史结果未记录集群校核范围，不能确认执行安全</span>
                )}
            </>
          )}
          {serviceStatus === 'offline' ? (
            <Button
              variant="ghost"
              size="sm"
              disabled={serviceChecking}
              onClick={recheckService}
            >
              <RefreshCw className={serviceChecking ? 'spin' : ''} />
              重新连接
            </Button>
          ) : (
            <span className="result-origin">
              {simulation
                ? `${regionDisplayName(simulation.metadata.region)} · ${simulation.metadata.steps} 时段 · 结果 ${completedSimulation?.simulationId}${['running', 'error', 'cancelled'].includes(runState) ? ' · 上次完成的结果' : ''}`
                : '尚无仿真结果'}
            </span>
          )}
        </div>
        {runState === 'error' && (
          <div className="error-message" role="alert">
            <AlertTriangle size={18} />
            <span>{runMessage}</span>
          </div>
        )}

        {selectedView === 'group_control' && groupControl && (
          <div className="scenario-picker">
            <Label htmlFor="group-scenario">回放场景</Label>
            <Select
              value={effectiveGroupScenario}
              onValueChange={(value) => {
                if (value) {
                  setGroupScenario(value);
                  setCursor(0);
                  setIsPlaying(false);
                }
              }}
            >
              <SelectTrigger id="group-scenario">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                {groupScenarios.map((id) => (
                  <SelectItem key={id} value={id}>
                    {groupScenarioLabels[id] ?? id}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
            <span>
              {selectedGroupRecords.length} 个原生周期 · 切换不重新求解
            </span>
          </div>
        )}

        <div className="metrics-grid">
          {scenarioMetrics.map((metric) => (
            <MetricCard key={metric.label} {...metric} />
          ))}
        </div>

        {(selectedView === 'overview' ||
          selectedView === 'single_microgrid') && (
          <RenewableEventControls
            scope={selectedView === 'overview' ? 'cluster' : 'single'}
            resultRegion={resultRegion}
            timeLabel={current.time}
            disabledReason={
              runState === 'running'
                ? '仿真运行中，完成后可注入事件。'
                : !simulation
                  ? '请先运行仿真，生成基线结果。'
                  : serviceStatus !== 'online'
                    ? '本地求解服务未连接，暂时无法注入事件。'
                    : !playbackData.length
                      ? '当前结果没有可用的回放时刻。'
                      : null
            }
            onInject={(kind, target) => {
              void runEventBranch(kind, target);
            }}
          />
        )}

        <div className="dashboard-grid">
          <section className="panel topology-panel">
            <div className="panel-title">
              <h3>{topologyTitle}</h3>
              <em className={outageNow ? 'fault-status' : ''}>
                {!simulation
                  ? '等待计算'
                  : isIterationView
                    ? `${current.time} · 原生记录`
                    : selectedView === 'group_control'
                      ? '原生分钟记录'
                      : '计划回放 · 非设备实绩'}
              </em>
            </div>
            {selectedView === 'single_microgrid' ? (
              <SingleMicrogridCanvas current={current} region={resultRegion} />
            ) : selectedView === 'group_control' ? (
              <GroupControlCanvas record={currentGroupRecord} />
            ) : (
              <div className="topology-canvas">
                <div className="grid-source">
                  <Zap size={25} />
                  <span>上级电网 / 协调层</span>
                  <b>
                    {isIterationView
                      ? 'ADMM 共识'
                      : `${numberLabel(current.import)} MW`}
                  </b>
                </div>
                <div className="trunk trunk-main" />
                <div className="trunk trunk-left" />
                <div className="trunk trunk-right" />
                {regions.map((region, index) => (
                  <RegionNode
                    key={region.id}
                    region={region}
                    index={index}
                    importMw={clusterPoint?.regional_import_mw[region.id]}
                    status={
                      !simulation
                        ? '等待仿真'
                        : iterationRecord?.fallback_regions.includes(region.id)
                          ? '自治降级'
                          : outageAtIteration(
                                simulation,
                                iterationRecord?.iteration ?? -1,
                                region.id,
                              )
                            ? '失联'
                            : isIterationView
                              ? '参与协调'
                              : '计划回放'
                    }
                  />
                ))}
                {playbackActive && (
                  <>
                    <div className="flow-pulse pulse-1" />
                    <div className="flow-pulse pulse-2" />
                    <div className="flow-pulse pulse-3" />
                  </>
                )}
              </div>
            )}
            <div className="capacity-row">
              <span>{capacityLabel}</span>
              {capacityPercent != null && (
                <Progress
                  aria-label={capacityLabel}
                  value={Math.min(100, Math.max(0, capacityPercent))}
                />
              )}
              <b>
                {isIterationView
                  ? `${iterationRecord?.fresh_region_count ?? '—'} / 3`
                  : clusterPoint
                    ? `${numberLabel(current.import)} / ${numberLabel(clusterPoint.import_limit_mw)} MW`
                    : selectedView === 'group_control'
                      ? `${numberLabel(capacityPercent, 1)}%`
                      : 'PCC 正值受电'}
              </b>
            </div>
          </section>

          <section
            className={`panel verdict-panel ${overallPassed === false ? 'has-failures' : ''}`}
          >
            <div className="panel-title">
              <h3>运行结论</h3>
              <ShieldCheck className="verdict-icon" size={26} />
            </div>
            <div className="verdict-main">
              <i>
                {overallPassed === false ? (
                  <AlertTriangle size={29} />
                ) : (
                  <ShieldCheck size={29} />
                )}
              </i>
              <div>
                <span>本次算例校核</span>
                <strong>
                  {simulation
                    ? overallPassed
                      ? '全部通过'
                      : '存在未通过项'
                    : '尚未校核'}
                </strong>
              </div>
            </div>
            <div className="validation-toolbar">
              <span>
                {simulation
                  ? `${validationPassed} / ${verdictItems.length} 项通过`
                  : '运行后生成证据'}
              </span>
              <button
                type="button"
                disabled={!simulation}
                aria-pressed={validationFilter === 'failed'}
                onClick={() =>
                  setValidationFilter((v) => (v === 'all' ? 'failed' : 'all'))
                }
              >
                {validationFilter === 'all' ? '仅看未通过' : '查看全部'}
              </button>
            </div>
            <ul>
              {verdictItems
                .filter(
                  (item) => validationFilter === 'all' || item.passed === false,
                )
                .map((item, index) => (
                  <li
                    className={
                      item.passed === false
                        ? 'failed'
                        : item.passed == null
                          ? 'pending'
                          : ''
                    }
                    key={`${item.scope}-${item.code}-${index}`}
                  >
                    <details>
                      <summary>
                        <span>
                          <i />
                          {item.scope && <small>{item.scope}</small>}
                          {item.label}
                        </span>
                        <b>
                          {item.passed == null
                            ? '待运行'
                            : item.passed
                              ? '通过'
                              : '未通过'}
                        </b>
                      </summary>
                      <p>{item.explanation || '本项尚无计算结果。'}</p>
                      <code>{item.code}</code>
                    </details>
                  </li>
                ))}
              {simulation &&
                validationFilter === 'failed' &&
                validationPassed === verdictItems.length && (
                  <li className="all-clear">本次算例没有未通过项。</li>
                )}
            </ul>
            <div className="cost-gap">
              <span>{conclusion.label}</span>
              <strong>
                {conclusion.value}
                <small>{conclusion.unit}</small>
              </strong>
            </div>
            <p className="evidence-note">
              当前结论仅适用于本次参数化算例，不构成现场安全许可。
            </p>
            {communication?.event_executions?.map((event) => (
              <p className="evidence-note" key={event.window.event_id}>
                {event.window.event_id} ·{' '}
                {event.window.scope === 'global'
                  ? '全局丢包'
                  : `${event.window.target} 失联`}
                {' · '}
                {event.status === 'executed'
                  ? '窗口已完整执行'
                  : event.status === 'partially_executed'
                    ? '窗口仅部分执行'
                    : '未运行到该窗口'}
                {' · '}覆盖 {event.observed_ticks.length} 轮，窗口内丢弃{' '}
                {event.dropped_while_active} 条
              </p>
            ))}
            <Button
              className="export-button"
              variant="outline"
              disabled={!simulation}
              onClick={exportResult}
            >
              <Download size={16} />
              导出本次结果 JSON
            </Button>
            {branchComparison.length > 0 && (
              <div className="branch-comparison">
                <div>
                  <span>原方案 / 事件分支</span>
                  <b>{lastEventLabel}</b>
                </div>
                {branchComparison.map(([label, before, after, unit]) => (
                  <div className="branch-row" key={label}>
                    <span>{label}</span>
                    <b>
                      {typeof before === 'number'
                        ? before.toFixed(unit === '元' ? 0 : 2)
                        : before}
                    </b>
                    <ChevronRight />
                    <strong>
                      {typeof after === 'number'
                        ? after.toFixed(unit === '元' ? 0 : 2)
                        : after}
                      <small>{unit}</small>
                    </strong>
                  </div>
                ))}
              </div>
            )}
          </section>

          <section className="panel chart-panel">
            <div className="panel-title">
              <h3>
                {selectedView === 'group_control'
                  ? '考核点功率与动态三重阈值'
                  : isIterationView
                    ? '分布式协调收敛过程'
                    : '负荷、新能源与 PCC 受电'}
              </h3>
              <div className="chart-legend">
                {selectedView === 'group_control' ? (
                  <>
                    <span>
                      <i className="risk" />
                      风险限值
                    </span>
                    <span>
                      <i className="load" />
                      安全阈值
                    </span>
                    <span>
                      <i className="renewable" />
                      恢复阈值
                    </span>
                    <span>
                      <i className="power" />
                      PCC
                    </span>
                  </>
                ) : isIterationView ? (
                  <>
                    <span>
                      <i className="load" />
                      原始残差
                    </span>
                    <span>
                      <i className="renewable" />
                      对偶残差
                    </span>
                    <span>
                      <i className="power" />
                      原始容差
                    </span>
                    <span>
                      <i className="dual" />
                      对偶容差
                    </span>
                  </>
                ) : (
                  <>
                    <span>
                      <i className="load" />
                      负荷
                    </span>
                    <span>
                      <i className="renewable" />
                      新能源
                    </span>
                    <span>
                      <i className="power" />
                      PCC 受电
                    </span>
                  </>
                )}
              </div>
            </div>
            <div className="chart-wrap">
              {mounted &&
              selectedView === 'group_control' &&
              groupChartData.length > 0 ? (
                <ResponsiveContainer width="100%" height="100%">
                  <ComposedChart
                    accessibilityLayer
                    data={groupChartData}
                    margin={{ top: 14, right: 16, bottom: 0, left: 0 }}
                  >
                    <CartesianGrid
                      stroke="#17384b"
                      strokeDasharray="4 6"
                      vertical={false}
                    />
                    <XAxis
                      dataKey="time"
                      stroke="#67879a"
                      tickLine={false}
                      axisLine={false}
                      fontSize={11}
                    />
                    <YAxis
                      stroke="#67879a"
                      tickLine={false}
                      axisLine={false}
                      fontSize={11}
                    />
                    <Tooltip
                      contentStyle={{
                        background: '#0b2231',
                        border: '1px solid #245069',
                        borderRadius: 8,
                        color: '#dcecf4',
                      }}
                    />
                    <Line
                      name="风险限值 (MW)"
                      type="stepAfter"
                      dataKey="risk"
                      stroke="#ff6d72"
                      dot={false}
                      strokeWidth={1.5}
                    />
                    <Line
                      name="安全阈值 (MW)"
                      type="stepAfter"
                      dataKey="safety"
                      stroke="#778bff"
                      dot={false}
                      strokeWidth={1.5}
                    />
                    <Line
                      name="恢复阈值 (MW)"
                      type="stepAfter"
                      dataKey="restore"
                      stroke="#f3c95d"
                      dot={false}
                      strokeWidth={1.5}
                    />
                    <Line
                      name="PCC 实绩 (MW)"
                      type="linear"
                      dataKey="pcc"
                      stroke="#39c9ff"
                      dot={{ r: 2 }}
                      strokeWidth={2}
                    />
                    <ReferenceLine
                      x={currentGroupRecord?.time_minute}
                      stroke="#9db2c7"
                      strokeDasharray="3 3"
                    />
                  </ComposedChart>
                </ResponsiveContainer>
              ) : mounted &&
                (selectedView === 'cluster_coordination' ||
                  selectedView === 'communication_fault') &&
                admmChartData.length ? (
                <ResponsiveContainer width="100%" height="100%">
                  <ComposedChart
                    accessibilityLayer
                    data={admmChartData}
                    margin={{ top: 14, right: 16, bottom: 0, left: 0 }}
                  >
                    <CartesianGrid
                      stroke="#17384b"
                      strokeDasharray="4 6"
                      vertical={false}
                    />
                    <XAxis
                      dataKey="iteration"
                      stroke="#67879a"
                      tickLine={false}
                      axisLine={false}
                      fontSize={11}
                    />
                    <YAxis
                      scale="log"
                      domain={['auto', 'auto']}
                      stroke="#67879a"
                      tickLine={false}
                      axisLine={false}
                      fontSize={11}
                    />
                    <Tooltip
                      contentStyle={{
                        background: '#0b2231',
                        border: '1px solid #245069',
                        borderRadius: 8,
                        color: '#dcecf4',
                      }}
                    />
                    <Line
                      name="原始残差"
                      type="linear"
                      dataKey="primal_residual"
                      stroke="#778bff"
                      dot={false}
                      strokeWidth={1.8}
                    />
                    <Line
                      name="对偶残差"
                      type="linear"
                      dataKey="dual_residual"
                      stroke="#f3c95d"
                      dot={false}
                      strokeWidth={1.8}
                    />
                    <Line
                      name="原始容差"
                      type="linear"
                      dataKey="primal_tolerance"
                      stroke="#39c9ff"
                      dot={false}
                      strokeDasharray="4 4"
                      strokeWidth={1.3}
                    />
                    <Line
                      name="对偶容差"
                      type="linear"
                      dataKey="dual_tolerance"
                      stroke="#45d6a4"
                      dot={false}
                      strokeDasharray="4 4"
                      strokeWidth={1.3}
                    />
                    <ReferenceLine
                      x={iterationRecord?.iteration}
                      stroke="#9db2c7"
                      strokeDasharray="3 3"
                    />
                  </ComposedChart>
                </ResponsiveContainer>
              ) : mounted &&
                !isIterationView &&
                selectedView !== 'group_control' &&
                chartData.length ? (
                <ResponsiveContainer width="100%" height="100%">
                  <ComposedChart
                    accessibilityLayer
                    data={chartData}
                    margin={{ top: 14, right: 16, bottom: 0, left: 0 }}
                  >
                    <defs>
                      <linearGradient
                        id="powerFill"
                        x1="0"
                        y1="0"
                        x2="0"
                        y2="1"
                      >
                        <stop
                          offset="0%"
                          stopColor="#39c9ff"
                          stopOpacity={0.32}
                        />
                        <stop
                          offset="100%"
                          stopColor="#39c9ff"
                          stopOpacity={0}
                        />
                      </linearGradient>
                    </defs>
                    <CartesianGrid
                      stroke="#17384b"
                      strokeDasharray="4 6"
                      vertical={false}
                    />
                    <XAxis
                      dataKey="time"
                      stroke="#67879a"
                      tickLine={false}
                      axisLine={false}
                      fontSize={11}
                    />
                    <YAxis
                      stroke="#67879a"
                      tickLine={false}
                      axisLine={false}
                      fontSize={11}
                    />
                    <Tooltip
                      contentStyle={{
                        background: '#0b2231',
                        border: '1px solid #245069',
                        borderRadius: 8,
                        color: '#dcecf4',
                      }}
                    />
                    <Area
                      name="PCC 受电 (MW)"
                      type="linear"
                      dataKey="import"
                      stroke="#39c9ff"
                      fill="url(#powerFill)"
                      strokeWidth={2}
                    />
                    <Line
                      name="负荷 (MW)"
                      type="linear"
                      dataKey="load"
                      stroke="#778bff"
                      dot={false}
                      strokeWidth={1.8}
                    />
                    <Line
                      name="新能源 (MW)"
                      type="linear"
                      dataKey="renewable"
                      stroke="#f3c95d"
                      dot={false}
                      strokeWidth={1.8}
                    />
                  </ComposedChart>
                </ResponsiveContainer>
              ) : (
                <div className="chart-empty">
                  <Activity size={32} />
                  <strong>
                    {runState === 'running'
                      ? '正在计算本场景'
                      : '运行后呈现算法轨迹'}
                  </strong>
                  <p>
                    {runState === 'running'
                      ? '阶段进度来自后台任务，完成后可逐点回放。'
                      : '使用上方运行按钮生成结果；此处不使用预设展示曲线。'}
                  </p>
                </div>
              )}
            </div>
          </section>
        </div>
      </section>

      <footer className="playback-bar">
        <div className="playback-controls">
          <Button
            disabled={!playbackData.length}
            aria-label="回到起点"
            variant="ghost"
            size="icon"
            onClick={() => {
              setCursor(0);
              setIsPlaying(false);
            }}
          >
            <RotateCcw />
          </Button>
          <Button
            disabled={playbackData.length < 2 || runState === 'running'}
            aria-label={playbackActive ? '暂停回放' : '开始回放'}
            className="play-button"
            size="icon"
            onClick={() => {
              if (cursor >= playbackData.length - 1) setCursor(0);
              setIsPlaying(!playbackActive);
            }}
          >
            {playbackActive ? <Pause /> : <Play />}
          </Button>
          <Button
            disabled={!playbackData.length || cursor >= playbackData.length - 1}
            aria-label={isIterationView ? '下一轮记录' : '下一分钟记录'}
            variant="ghost"
            size="icon"
            onClick={() => {
              setIsPlaying(false);
              setCursor((value) =>
                Math.min(value + 1, playbackData.length - 1),
              );
            }}
          >
            <FastForward />
          </Button>
          <button
            className="speed-button"
            aria-label={`回放速度 ${speed} 倍，点击切换`}
            type="button"
            disabled={!playbackData.length}
            onClick={() =>
              setSpeed((value) =>
                value === 1 ? 5 : value === 5 ? 15 : value === 15 ? 60 : 1,
              )
            }
          >
            {speed}×
          </button>
        </div>
        <div className="timeline">
          <span>{playbackData[0]?.time ?? '—'}</span>
          <div className="timeline-track">
            <Slider
              aria-label={
                isIterationView ? '协调迭代回放位置' : '仿真分钟回放位置'
              }
              disabled={playbackData.length < 2}
              min={0}
              max={Math.max(1, playbackData.length - 1)}
              step={1}
              value={[Math.min(cursor, Math.max(0, playbackData.length - 1))]}
              onValueChange={(value) => {
                setCursor(typeof value === 'number' ? value : value[0]);
                setIsPlaying(false);
              }}
            />
            {eventMarkerPercent != null && (
              <em
                title="事件分支注入点"
                style={{ left: `${eventMarkerPercent}%` }}
              />
            )}
          </div>
          <span>{playbackData.at(-1)?.time ?? '—'}</span>
        </div>
        <div className="current-time">
          <span>
            {isIterationView
              ? '原生迭代 · 不插值'
              : simulation?.group_control
                ? '群控原生分钟'
                : simulation
                  ? '区间插值 · 非新增实绩'
                  : '等待仿真'}
          </span>
          <strong>{current.time}</strong>
        </div>
      </footer>
    </main>
  );
}
