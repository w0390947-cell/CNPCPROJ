'use client';

import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { useRouter } from 'next/navigation';
import {
  cancelSimulation,
  createSimulation,
  getHealth,
  getSimulation,
  getSingleExampleLibrary,
  getSingleExampleRecord,
  getSimulationResult,
  SimulationCancelledError,
  waitForSimulation,
  type JobStatus,
  type SimulationConfiguration,
  type SimulationResult,
} from '@/lib/simulation-api';
import { jobProgressPresentation } from '@/features/simulation-progress';
import {
  PlatformHeader,
  PlatformNavigation,
} from '@/shared/ui/platform-chrome';
import { completedExecution, LiveExecution, useLiveExecution } from './live';
import {
  codedRegionDisplay,
  codedRegionText,
} from '../../shared/lib/coded-region-display';
import workspace from '../../shared/ui/scientific-workspace.module.css';
import { SavedRecordSelector } from '../../shared/ui/saved-record-selector';
import styles from './case-page.module.css';
import exampleStyles from '../patent2/style.module.css';
import { SingleProvenance } from './provenance';
import type { SingleExampleInputs } from '../../shared/api/generated/single-example';

const CASE_CONFIGURATION: SimulationConfiguration = {
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

/** A restored task must actually belong to the fixed example, not just its route. */
export function isCoreExample(result: SimulationResult): boolean {
  const request = result.request as typeof result.request & {
    single_boundary?: unknown;
    single_research?: unknown;
  };
  const e = result.single_execution;
  const u = e?.unified;
  if (
    result.metadata.scenario_type !== 'single_microgrid' ||
    result.metadata.region !== 'SC' ||
    result.metadata.steps !== 8 ||
    result.metadata.dt_hours !== 3 ||
    request.region !== 'SC' ||
    request.steps !== 8 ||
    !request.storage_enabled ||
    (request.events?.length ?? 0) !== 0 ||
    request.single_boundary != null ||
    request.single_research != null ||
    !e ||
    e.total_minutes !== 1440
  )
    return false;
  if (!u) return true; // A failed reference still reports its actual reason.
  const devices = u.domain.devices.resources;
  const task = u.domain.energy_task;
  const net = u.domain.network;
  const research = u.domain.research;
  const wind = devices.filter((r) => r.kind === 'wind');
  const pv = devices
    .filter((r) => r.kind === 'pv')
    .sort((a, b) => a.p.high - b.p.high);
  const storage = devices.find((r) => r.kind === 'storage');
  const svg = devices.findIndex((r) => r.kind === 'svg');
  const near = (a: number | undefined, b: number) =>
    a != null && Math.abs(a - b) < 1e-9;
  return (
    u.requested_steps === 1440 &&
    u.domain.network.buses.length === 10 &&
    u.domain.network.branches.length === 9 &&
    wind.length === 2 &&
    wind.every((r) => near(r.p.high, 5)) &&
    pv.length === 2 &&
    near(pv[0].p.high, 1.8) &&
    near(pv[1].p.high, 2.4) &&
    devices.filter((r) => r.kind === 'storage').length === 1 &&
    near(storage?.p.high, 2.5) &&
    devices.filter((r) => r.kind === 'svg').length === 1 &&
    near(devices[svg].q.low, -1.8) &&
    near(devices[svg].q.high, 1.8) &&
    near(u.initial.energy_mwh, 2.5) &&
    u.initial.p_mw.every((p) => near(p, 0)) &&
    u.initial.q_mvar.every((q, i) => near(q, i === svg ? 1.8 : 0)) &&
    u.domain.devices.step_minutes === 1 &&
    u.update_steps === 15 &&
    u.domain.response_steps === 10 &&
    near(u.domain.p_band_mw, 0.05) &&
    near(u.domain.q_band_mvar, 0.05) &&
    near(net.pcc_min_mw, 0.5) &&
    near(net.pcc_max_mw, 18) &&
    near(net.voltage_min_pu, 0.95) &&
    near(net.voltage_max_pu, 1.05) &&
    near(net.pf_min, 0.9) &&
    near(research.load_relative_error, 0.025) &&
    near(research.wind_nameplate_error, 0.05) &&
    near(research.pv_nameplate_error, 0.15) &&
    research.forecast_interpolation === 'linear' &&
    task != null &&
    task.deadline_step === 1440 &&
    Math.abs(task.energy_mwh.low - 2.45) < 1e-9 &&
    Math.abs(task.energy_mwh.high - 2.55) < 1e-9
  );
}

const assessment = (status?: string) =>
  status === 'passed'
    ? '通过'
    : status === 'violated' || status === 'rejected'
      ? '未通过'
      : '未确认';
export default function SingleCasePage() {
  const router = useRouter();
  const [service, setService] = useState<'checking' | 'online' | 'offline'>(
    'checking',
  );
  const [state, setState] = useState<
    'ready' | 'running' | 'success' | 'error' | 'cancelled'
  >('ready');
  const [message, setMessage] = useState('');
  const [status, setStatus] = useState<JobStatus | null>(null);
  const [completed, setCompleted] = useState<{
    id: string;
    result: SimulationResult;
    sourceLabel: string;
  } | null>(null);
  const [cancelling, setCancelling] = useState(false);
  const active = useRef<string | null>(null);
  const lock = useRef(false);
  const subscription = useRef<AbortController | null>(null);
  const [library, setLibrary] = useState<'core'[]>([]);
  const [loading, setLoading] = useState(false);
  const [refreshing, setRefreshing] = useState(false);
  const [libraryError, setLibraryError] = useState('');
  const refreshEpoch = useRef(0);
  const [preview, setPreview] = useState<SingleExampleInputs | null>(null);

  useEffect(() => {
    let alive = true;
    const check = () =>
      getHealth().then(
        (h) => {
          if (alive) setService(h.status === 'ok' ? 'online' : 'offline');
        },
        () => {
          if (alive) setService('offline');
        },
      );
    void check();
    const timer = window.setInterval(check, 15000);
    return () => {
      alive = false;
      window.clearInterval(timer);
      subscription.current?.abort();
      refreshEpoch.current += 1;
    };
  }, []);

  const observe = useCallback(async (id?: string) => {
    if (lock.current) return;
    lock.current = true;
    const controller = new AbortController();
    subscription.current = controller;
    setState('running');
    setMessage(id ? '正在恢复实施例任务…' : '正在创建实施例任务…');
    setStatus(null);
    setLibraryError('');
    try {
      const job = id
        ? await getSimulation(id)
        : await createSimulation('single_microgrid', CASE_CONFIGURATION, []);
      controller.signal.throwIfAborted();
      active.current = job.simulation_id;
      setStatus(job);
      const url = new URL(window.location.href);
      url.searchParams.set('simulation', job.simulation_id);
      window.history.replaceState(null, '', url);
      const result =
        job.state === 'succeeded'
          ? await getSimulationResult(job.simulation_id)
          : await waitForSimulation(
              job,
              (update) => {
                setStatus(update);
                setMessage(update.stage_label ?? '正在计算实施例…');
              },
              350,
              controller.signal,
            );
      controller.signal.throwIfAborted();
      if (!isCoreExample(result))
        throw new Error(
          '该任务不属于本页面的 8.2 固定实施例，请运行本实施例。',
        );
      setCompleted({
        id: job.simulation_id,
        result,
        sourceLabel: job.name || '当前后台任务',
      });
      setState('success');
      setMessage(
        job.name?.startsWith('历史仿真记录')
          ? '已载入历史仿真记录'
          : '实施例计算完成',
      );
    } catch (error) {
      if (controller.signal.aborted) return;
      if (error instanceof SimulationCancelledError) {
        setState('cancelled');
        setMessage('实施例计算已取消');
      } else {
        setState('error');
        setMessage(error instanceof Error ? error.message : '实施例计算失败');
      }
      // A failed task must not masquerade as the previously visible result.
      const url = new URL(window.location.href);
      url.searchParams.delete('simulation');
      window.history.replaceState(null, '', url);
    } finally {
      lock.current = false;
      active.current = null;
      setCancelling(false);
    }
  }, []);

  const refresh = useCallback(async () => {
    const epoch = ++refreshEpoch.current;
    setRefreshing(true);
    setLibraryError('');
    try {
      const records = await getSingleExampleLibrary();
      if (epoch === refreshEpoch.current) setLibrary(records);
    } catch (error) {
      if (epoch === refreshEpoch.current)
        setLibraryError(
          error instanceof Error
            ? error.message
            : '无法读取保存记录，请检查仿真服务',
        );
    } finally {
      if (epoch === refreshEpoch.current) setRefreshing(false);
    }
  }, []);

  const loadSaved = useCallback(async () => {
    if (lock.current) return;
    lock.current = true;
    const controller = new AbortController();
    subscription.current = controller;
    setLoading(true);
    setLibraryError('');
    try {
      const result = await getSingleExampleRecord();
      controller.signal.throwIfAborted();
      const execution = result.single_execution;
      if (
        !isCoreExample(result) ||
        !execution?.unified ||
        execution.completed_minutes !== execution.total_minutes
      )
        throw new Error('该保存记录不是本页面固定实施例的完整计算结果。');
      setCompleted({
        id: 'library:core',
        result,
        sourceLabel: 'SC 单微网 · 已保存示范计算',
      });
      setStatus(null);
      setState('success');
      setMessage('已载入保存的示范计算');
      const url = new URL(window.location.href);
      url.searchParams.set('simulation', 'library:core');
      window.history.replaceState(null, '', url);
    } catch (error) {
      if (!controller.signal.aborted)
        setLibraryError(
          error instanceof Error ? error.message : '无法读取示范计算',
        );
    } finally {
      lock.current = false;
      if (!controller.signal.aborted) setLoading(false);
    }
  }, []);

  useEffect(() => {
    let alive = true;
    void Promise.resolve().then(() => {
      if (alive) void refresh();
    });
    return () => {
      alive = false;
    };
  }, [refresh]);

  useEffect(() => {
    const id = new URL(window.location.href).searchParams.get('simulation');
    if (!id) return;
    let alive = true;
    void Promise.resolve().then(() => {
      if (alive) void (id === 'library:core' ? loadSaved() : observe(id));
    });
    return () => {
      alive = false;
    };
  }, [loadSaved, observe]);

  async function cancel() {
    if (!active.current || cancelling) return;
    setCancelling(true);
    setMessage('正在取消实施例计算…');
    try {
      setStatus(await cancelSimulation(active.current));
    } catch (error) {
      setCancelling(false);
      setMessage(error instanceof Error ? error.message : '取消失败');
    }
  }

  const progress = jobProgressPresentation(status, state === 'running');
  const live = useLiveExecution(status);
  const result = completed?.result ?? null;
  const completedView = useMemo(
    () =>
      completed ? completedExecution(completed.id, completed.result) : null,
    [completed],
  );
  const isCurrentResult =
    !!completed &&
    (status?.simulation_id === completed.id ||
      (completed.id === 'library:core' &&
        status === null &&
        state === 'success'));
  const finalView = isCurrentResult ? completedView : null;
  const displayView = useMemo(
    () => codedRegionDisplay(finalView ?? live),
    [finalView, live],
  );
  const displayResult = useMemo(() => codedRegionDisplay(result), [result]);
  const displayStatus = useMemo(() => codedRegionDisplay(status), [status]);
  const u = result?.single_execution?.unified;
  const improvement = result?.executive_summary.economic_improvement_percent;
  return (
    <main className={`control-room ${workspace.scope}`}>
      <PlatformHeader status={service} />
      <PlatformNavigation
        activeItem="single_microgrid"
        onNavigate={(item) => {
          if (item.id !== 'single_microgrid') router.push(item.href);
        }}
      />
      <section
        className={`workspace ${styles.workspace} ${exampleStyles.main}`}
      >
        <div className={workspace.heading}>
          <div>
            <h1>新能源最优协同控制算法仿真</h1>
            <p>展示设备响应、PCC 目标跟踪、原期限电量任务与网络校核。</p>
          </div>
        </div>
        <section className={exampleStyles.panel} aria-label="计算与保存记录">
          <div className={styles.status} aria-live="polite">
            <button
              className={exampleStyles.primary}
              disabled={
                state === 'running'
                  ? !status || cancelling
                  : service !== 'online' || loading
              }
              onClick={() =>
                state === 'running' ? void cancel() : void observe()
              }
            >
              {state === 'running'
                ? cancelling
                  ? '正在取消…'
                  : '取消仿真'
                : '现场重新计算'}
            </button>
            <span>
              {state === 'ready'
                ? service === 'offline'
                  ? '请先启动仿真服务'
                  : codedRegionText(message) ||
                    (service === 'checking'
                      ? '正在连接仿真服务'
                      : '请选择保存的示范计算，或现场重新计算')
                : codedRegionText(message)}
            </span>
            {status && (
              <span>
                {progress.stateLabel} · 流程进度 {progress.percent}%
              </span>
            )}
            {progress.detail && <span>{codedRegionText(progress.detail)}</span>}
            {state === 'running' && (
              <progress
                aria-label="仿真流程进度"
                max={100}
                value={progress.percent}
              />
            )}
            {result && !isCurrentResult && (
              <span>
                当前展示的新任务与上一次完成的结果相互独立。
                <a
                  href={`/single-microgrid?simulation=${encodeURIComponent(completed!.id)}`}
                >
                  查看上一次完成的结果
                </a>
              </span>
            )}
          </div>
          <SavedRecordSelector
            className={exampleStyles.controls}
            records={library.map(() => ({
              id: 'library:core',
              label: 'SC 单微网 · 已保存示范计算',
            }))}
            loading={loading}
            disabled={state === 'running'}
            refreshing={refreshing}
            onSelect={() => void loadSaved()}
            onRefresh={() => void refresh()}
          />
          {libraryError && (
            <p role="alert" className={styles.error}>
              {codedRegionText(libraryError)}
            </p>
          )}
        </section>
        {state === 'error' && (
          <p role="alert" className={styles.error}>
            {codedRegionText(message)}
          </p>
        )}
        {
          <LiveExecution
            onPreview={setPreview}
            key={
              status?.simulation_id ??
              (isCurrentResult ? completed.id : 'preview')
            }
            data={displayView}
            status={displayStatus}
            result={isCurrentResult ? displayResult : null}
            sourceLabel={
              isCurrentResult
                ? codedRegionText(completed?.sourceLabel ?? '')
                : undefined
            }
            generatedAt={
              isCurrentResult ? result?.metadata.generated_at : undefined
            }
            summary={
              isCurrentResult && result ? (
                <section
                  className={exampleStyles.panel}
                  aria-label="实施例运行结果"
                >
                  <h2>执行结果</h2>
                  <div className={styles.cards}>
                    <div>
                      <span>条件安全</span>
                      <strong data-status={u?.status}>
                        {assessment(u?.status)}
                      </strong>
                    </div>
                    <div>
                      <span>目标接纳</span>
                      <strong data-status={u?.target_admission_status}>
                        {assessment(u?.target_admission_status)}
                      </strong>
                    </div>
                    <div>
                      <span>期限后跟踪</span>
                      <strong data-status={u?.tracking_status}>
                        {assessment(u?.tracking_status)}
                      </strong>
                    </div>
                    <div>
                      <span>储能原期限任务</span>
                      <strong data-status={u?.hard_task?.outcome}>
                        {u?.hard_task?.outcome === 'met'
                          ? '已完成'
                          : u?.hard_task?.outcome === 'missed'
                            ? '未完成'
                            : '未确认'}
                      </strong>
                      <small>
                        保证：
                        {u?.hard_task?.guarantee === 'maintained'
                          ? '持续保持'
                          : '未确认'}
                      </small>
                    </div>
                    <div>
                      <span>计划成本改善</span>
                      <strong>
                        {improvement == null
                          ? '无可用数据'
                          : `${improvement.toFixed(2)}%`}
                      </strong>
                    </div>
                  </div>
                  {result.single_execution?.reason && (
                    <p>{codedRegionText(result.single_execution.reason)}</p>
                  )}
                </section>
              ) : null
            }
          />
        }
        <SingleProvenance
          runState={state}
          result={isCurrentResult ? result : null}
          initial={(finalView ?? live)?.initial}
          preview={preview}
          recordId={isCurrentResult ? completed.id : status?.simulation_id}
          sourceLabel={isCurrentResult ? completed.sourceLabel : status?.name}
        />
      </section>
    </main>
  );
}
