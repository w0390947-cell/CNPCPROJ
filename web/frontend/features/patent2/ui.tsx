'use client';
import { useEffect, useMemo, useRef, useState } from 'react';
import {
  codedRegionDisplay,
  codedRegionText,
} from '../../shared/lib/coded-region-display';
import { useRouter } from 'next/navigation';
import { Download, Pause, Play, SkipBack, SkipForward } from 'lucide-react';
import { InputCurves } from '../../shared/ui/input-curves';
import { clusterInputRegions } from '../../shared/lib/input-series';
import type { SimulationResult, JobStatus } from '@/lib/simulation-api';
import type { Patent2Inputs } from '@/shared/api/generated/patent2';
import {
  PlatformHeader,
  PlatformNavigation,
} from '@/shared/ui/platform-chrome';
import { api, HttpError, RecordError, type Scenario } from './client';
import { Inputs } from './inputs';
import { useClusterLive } from './live';
import { executionView } from './model';
import { Devices, Evidence, Overview, Reserve, Rolling } from './execution';
import {
  keyMinutes,
  labels,
  metrics,
  number,
  scenarioLabels,
  sourceLabels,
  states,
  statusLabels,
} from './model';
import workspace from '../../shared/ui/scientific-workspace.module.css';
import { SavedRecordSelector } from '../../shared/ui/saved-record-selector';
import styles from './style.module.css';

type Saved = { result: SimulationResult; source: string; jobId?: string };
const tabs = [
  '共同条件',
  '规划决策',
  '执行监控',
  '专项任务',
  '证据与结论',
] as const;
const active = (job: JobStatus | null) =>
  job?.state === 'queued' || job?.state === 'running';
const jobLabels = {
  queued: '排队中',
  running: '实际计算中',
  succeeded: '计算完成',
  cancelled: '已取消',
  failed: '计算失败',
  interrupted: '计算已中断',
};
const errorText = (error: unknown) =>
  error instanceof Error ? error.message : String(error);
function download(value: string, name: string) {
  const url = URL.createObjectURL(
    new Blob([value], { type: 'application/json' }),
  );
  const a = document.createElement('a');
  a.href = url;
  a.download = name;
  a.click();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}

export default function Patent2Page() {
  const router = useRouter(),
    loadSequence = useRef(0);
  const [selectedScenario, setScenario] = useState<Scenario>('normal'),
    [preview, setPreview] = useState<Patent2Inputs | null>(null);
  const [records, setRecords] = useState<Partial<Record<Scenario, Saved>>>({});
  const [job, setJob] = useState<JobStatus | null>(null),
    [jobId, setJobId] = useState<string | null>(null);
  const [error, setError] = useState(''),
    [service, setService] = useState<'checking' | 'online' | 'offline'>(
      'checking',
    );
  const [library, setLibrary] = useState<Scenario[]>([]);
  const [loading, setLoading] = useState(false),
    [starting, setStarting] = useState(false),
    [cancelling, setCancelling] = useState(false);
  const [selectedMinute, setMinute] = useState(0),
    [playing, setPlaying] = useState(false),
    [speed, setSpeed] = useState(600),
    [tab, setTab] = useState(0);
  const [following, setFollowing] = useState(false);
  const live = useClusterLive(job);
  const scenario = live?.inputs?.scenario ?? selectedScenario;
  const observedJobId = job?.simulation_id;
  const saved = records[scenario];
  const result =
    job && saved?.jobId !== job.simulation_id ? undefined : saved?.result;
  const displayResult = useMemo(() => codedRegionDisplay(result), [result]);
  const displayLive = useMemo(
    () => codedRegionDisplay(live?.view),
    [live?.view],
  );
  const execution = displayResult?.cluster_execution
    ? executionView(displayResult.cluster_execution)
    : displayLive;
  const inputs =
    result?.patent2_evidence?.inputs ??
    live?.inputs ??
    (!job && preview?.scenario === scenario ? preview : null);
  const displayInputs = useMemo(() => codedRegionDisplay(inputs), [inputs]);
  const inputRegions = useMemo(() => clusterInputRegions(displayInputs, execution), [displayInputs, execution]);
  const busy = starting || !!jobId || active(job),
    steps = execution?.safety?.steps ?? [],
    end = steps.length;
  const minute = following ? end : Math.min(selectedMinute, end);
  const select = (n: number) => {
    setFollowing(false);
    setMinute(Math.max(0, Math.min(end, n)));
    setPlaying(false);
  };
  const showInputOutcome = (nextTab: number, target: string) => {
    const page = document;
    setTab(nextTab);
    window.setTimeout(() => page.getElementById(target)?.scrollIntoView?.({ block: 'start' }), 0);
  };

  function install(r: SimulationResult, source: string, completedJob?: string) {
    const s = r.patent2_evidence!.inputs.scenario;
    setRecords((previous) => ({
      ...previous,
      [s]: { result: r, source, jobId: completedJob },
    }));
    setScenario(s);
    if (!completedJob) {
      setMinute(0);
      setFollowing(false);
      setPlaying(false);
    }
  }
  async function refresh() {
    try {
      setLibrary(await api.library());
      setService('online');
    } catch (e) {
      setError(errorText(e));
      setService('offline');
    }
  }
  useEffect(() => {
    let live = true;
    const sequence = loadSequence.current;
    const id =
      new URLSearchParams(window.location.search).get('simulation') ??
      localStorage.getItem('patent2-active');
    if (id?.startsWith('library:'))
      void api
        .result(id)
        .then((r) => {
          if (live && sequence === loadSequence.current)
            install(r, `服务器保存记录 · ${id}`);
        })
        .catch((e) => {
          if (live) setError(errorText(e));
        });
    else if (id)
      void api
        .status(id)
        .then((status) => {
          if (live && sequence === loadSequence.current) {
            setJob(status);
            setJobId(id);
            setFollowing(active(status));
          }
        })
        .catch((e) => {
          if (live) {
            setError(errorText(e));
            localStorage.removeItem('patent2-active');
          }
        });
    void api
      .library()
      .then((values) => {
        if (live) {
          setLibrary(values);
          setService('online');
        }
      })
      .catch((e) => {
        if (live) {
          setError(errorText(e));
          setService('offline');
        }
      });
    return () => {
      live = false;
    };
  }, []);
  useEffect(() => {
    if (records[scenario] && !observedJobId) return;
    let live = true;
    api
      .inputs(scenario)
      .then((v) => {
        if (live) {
          setPreview(v);
          setService('online');
        }
      })
      .catch((e) => {
        if (live) {
          setService('offline');
          setError(errorText(e));
        }
      });
    return () => {
      live = false;
    };
  }, [scenario, records, observedJobId]);
  useEffect(() => {
    if (!jobId) return;
    let live = true;
    let timer: ReturnType<typeof setTimeout>;
    async function poll() {
      try {
        const status = await api.status(jobId!);
        if (!live) return;
        setJob(status);
        setService('online');
        setError('');
        if (active(status)) {
          timer = setTimeout(poll, 2000);
          return;
        }
        if (status.state === 'succeeded') {
          const r = await api.result(jobId!);
          if (!live) return;
          install(r, `服务器真实运行 · ${jobId}`, jobId!);
        } else setError(status.error_message ?? jobLabels[status.state]);
        if (!live) return;
        localStorage.removeItem('patent2-active');
        setJobId(null);
        setCancelling(false);
        void refresh();
      } catch (e) {
        if (live) {
          if (
            e instanceof RecordError ||
            (e instanceof HttpError && [404, 410, 422].includes(e.status))
          ) {
            setError(errorText(e));
            localStorage.removeItem('patent2-active');
            setJobId(null);
            setJob(null);
            setCancelling(false);
          } else {
            setError(`读取任务暂时失败，将继续重试：${errorText(e)}`);
            setService('offline');
            timer = setTimeout(poll, 5000);
          }
        }
      }
    }
    void poll();
    return () => {
      live = false;
      clearTimeout(timer);
    };
  }, [jobId]);
  useEffect(() => {
    if (!playing) return;
    const timer = setInterval(
      () =>
        setMinute((m) => {
          if (m >= end) {
            setPlaying(false);
            return m;
          }
          return m + 1;
        }),
      speed,
    );
    return () => clearInterval(timer);
  }, [playing, speed, end]);
  async function run() {
    setStarting(true);
    setPlaying(false);
    setError('');
    loadSequence.current++;
    try {
      const status = await api.run(scenario);
      setJob(status);
      setJobId(status.simulation_id);
      setMinute(0);
      setFollowing(true);
      setTab(2);
      localStorage.setItem('patent2-active', status.simulation_id);
      window.history.replaceState(
        null,
        '',
        `/patent2?simulation=${status.simulation_id}`,
      );
    } catch (e) {
      setError(errorText(e));
    } finally {
      setStarting(false);
    }
  }
  async function load(id: string) {
    if (!id) return;
    const sequence = ++loadSequence.current;
    setLoading(true);
    setError('');
    setPlaying(false);
    try {
      const r = await api.result(id);
      if (sequence !== loadSequence.current) return;
      install(r, `服务器保存记录 · ${id}`);
      setJob(null);
      window.history.replaceState(null, '', `/patent2?simulation=${id}`);
    } catch (e) {
      if (sequence === loadSequence.current) setError(errorText(e));
    } finally {
      if (sequence === loadSequence.current) setLoading(false);
    }
  }
  return (
    <div className={`control-room ${workspace.scope}`}>
      <PlatformHeader status={service} />
      <PlatformNavigation
        activeItem="patent2"
        onNavigate={(item) => router.push(item.href)}
      />
      <main className={`workspace ${styles.main}`}>
        <header className={workspace.heading}>
          <div>
            <h1>多层级微电网集群控制算法仿真</h1>
            <p>
              通过正常初始电量与低电量两种工况，展示三区域滚动规划、分钟响应与双向备用恢复的过程和结果。
            </p>
          </div>
        </header>
        <section className={styles.panel} aria-label="运行与回放">
          <div className={styles.controls}>
            <div className={styles.switcher}>
              {(['normal', 'low_energy'] as Scenario[]).map((s) => (
                <button
                  key={s}
                  aria-pressed={scenario === s}
                  disabled={busy || loading}
                  onClick={() => {
                    loadSequence.current++;
                    setScenario(s);
                    setTab(0);
                    setMinute(0);
                    setPlaying(false);
                    setJob(null);
                    setError('');
                  }}
                >
                  {scenarioLabels[s]}
                </button>
              ))}
            </div>
            <button
              className={styles.primary}
              disabled={busy || loading}
              onClick={() => void run()}
            >
              <Play size={16} />
              {busy ? '正在实际计算…' : '现场重新计算'}
            </button>
            {busy && (
              <button
                disabled={cancelling || !jobId}
                onClick={async () => {
                  setCancelling(true);
                  try {
                    setJob(await api.cancel(jobId!));
                  } catch (e) {
                    setError(errorText(e));
                    setCancelling(false);
                  }
                }}
              >
                {cancelling ? '正在取消…' : '取消计算'}
              </button>
            )}
            {result && (
              <button
                disabled={loading}
                onClick={() =>
                  download(
                    JSON.stringify(result),
                    `patent2-${scenario}-${result.metadata.generated_at.replaceAll(/[:.]/g, '-')}.json`,
                  )
                }
              >
                <Download size={16} />
                导出完整记录
              </button>
            )}
          </div>
          <SavedRecordSelector
            className={styles.controls}
            records={library.map((s) => ({
              id: `library:${s}`,
              label: `${scenarioLabels[s]} · 已保存示范计算`,
            }))}
            loading={loading}
            disabled={busy}
            onSelect={(id) => void load(id)}
            onRefresh={() => void refresh()}
          />
          {job && (
            <output className={styles.notice}>
              <b>{jobLabels[job.state]}</b> ·{' '}
              {codedRegionText(job.stage_label ?? '')}
              {job.rolling && (
                <span>
                  {' '}
                  · 窗口 {job.rolling.current_window}/
                  {job.rolling.total_windows}，已完成{' '}
                  {job.rolling.completed_windows} 个窗口，
                  {job.rolling.start_minute}—{job.rolling.end_minute} min
                </span>
              )}
              {busy && result && (
                <span> · 当前下方仍显示上次保存结果，新结果完成后替换。</span>
              )}
            </output>
          )}
          {error && (
            <div role="alert" className={styles.error}>
              {codedRegionText(error)}
              <button aria-label="关闭提示" onClick={() => setError('')}>
                ×
              </button>
            </div>
          )}
        </section>
        {result && (
          <>
            <div className={styles.provenance}>
              <b>{scenarioLabels[scenario]}</b> ·{' '}
              {codedRegionText(saved?.source ?? '')} · 生成于{' '}
              {new Date(result.metadata.generated_at).toLocaleString('zh-CN')}
            </div>
            <Overview result={displayResult!} />
          </>
        )}
        {!result && job && (
          <section className={styles.panel} aria-label="实时执行摘要">
            <h2>
              本次仿真 · 已完成 {end} / {live?.total || 60} 分钟
            </h2>
            <p>
              {active(job)
                ? '实时跟踪当前任务。'
                : '当前任务已停止，保留已收到的执行记录。'}
              整体结论与序列化证据复验尚未完成；局部校核通过不代表全程通过。
            </p>
            {live?.error && <p role="alert">{codedRegionText(live.error)}</p>}
          </section>
        )}
        {execution && (
          <section
            className={`${styles.panel} ${styles.player}`}
            aria-label="执行记录播放器"
          >
            <div className={styles.controls}>
              <button
                onClick={() => {
                  setPlaying(false);
                  setFollowing(true);
                }}
                disabled={following}
              >
                跟随最新节点
              </button>
              <button
                onClick={() => select(minute - 1)}
                disabled={minute === 0}
                aria-label="上一分钟"
              >
                <SkipBack size={16} />
              </button>
              <button
                className={styles.primary}
                onClick={() => {
                  setFollowing(false);
                  setMinute(minute >= end ? 0 : minute);
                  setPlaying(!playing);
                }}
                disabled={!end}
              >
                {playing ? <Pause size={16} /> : <Play size={16} />}{' '}
                {playing ? '暂停回放' : '播放记录'}
              </button>
              <button
                onClick={() => select(minute + 1)}
                disabled={minute >= end}
                aria-label="下一分钟"
              >
                <SkipForward size={16} />
              </button>
              <b>
                {minute} / {end} min
              </b>
              <label>
                速度{' '}
                <select
                  value={speed}
                  onChange={(e) => setSpeed(Number(e.target.value))}
                >
                  <option value={1200}>慢速</option>
                  <option value={600}>标准</option>
                  <option value={200}>快速</option>
                </select>
              </label>
            </div>
            <input
              className={styles.slider}
              aria-label="完成节点分钟"
              type="range"
              min={0}
              max={end}
              value={minute}
              onChange={(e) => select(Number(e.target.value))}
            />
            <div className={styles.controls}>
              <span>关键节点</span>
              {keyMinutes(execution).map((m) => (
                <button
                  key={m}
                  aria-pressed={minute === m}
                  onClick={() => select(m)}
                >
                  {m} min
                </button>
              ))}
            </div>
            <div className={styles.timeline} aria-label="实际动作来源">
              {steps.map((s) => (
                <button
                  key={s.minute}
                  data-kind={s.source}
                  aria-pressed={minute === s.minute + 1}
                  aria-label={`${s.minute}到${s.minute + 1}分钟：${sourceLabels[s.source]}`}
                  title={`${s.minute}→${s.minute + 1} min · ${sourceLabels[s.source]}`}
                  onClick={() => select(s.minute + 1)}
                />
              ))}
            </div>
            <p className={styles.muted}>
              区间动作：绿色为经济动作，橙色为认证后备，紫色为其他认证动作。点击色带跳转到对应完成节点。
            </p>
          </section>
        )}
        <nav className={workspace.tabs} aria-label="实施例讲解步骤">
          {tabs.map((t, i) => (
            <button
              key={t}
              aria-current={tab === i ? 'step' : undefined}

              onClick={() => setTab(i)}
            >
              <span>0{i + 1}</span>
              {t}
            </button>
          ))}
        </nav>
        {(tab === 0 || tab === 2) && inputRegions.length > 0 && (
          <InputCurves
            regions={inputRegions}
            minute={minute}
            completed={end}
            onSelect={select}
            onPlanning={() => showInputOutcome(1, 'cluster-planning')}
            onResponse={() => showInputOutcome(2, 'cluster-device-response')}
            decision={minute > 0 && steps[minute - 1] ? `本区间实际采用：${sourceLabels[steps[minute - 1].source]}。规划目标及窗口采纳情况可在“规划决策”查看。` : undefined}
          />
        )}
        {tab === 0 &&
          (inputs ? (
            <Inputs inputs={displayInputs!} />
          ) : (
            <section className={styles.panel}>
              <p>正在读取共同条件。若长时间未显示，请检查仿真服务连接。</p>
            </section>
          ))}
        {execution && tab === 1 && (
          <Rolling
            execution={execution}
            history={displayResult?.admm_history}
            minute={minute}
            onSelect={select}
          />
        )}
        {execution && tab === 2 && (
          <Devices execution={execution} minute={minute} onSelect={select} />
        )}
        {execution && tab === 3 && (
          <Reserve
            final={!!result}
            execution={execution}
            minute={minute}
            onSelect={select}
          />
        )}
        {displayResult && tab === 4 && <Evidence result={displayResult} />}
        {tab > 0 && !execution && (
          <section className={styles.panel}>
            <p>
              当前尚无已完成执行记录。运行期间将显示真实提交的分钟节点；规划求解期间请查看上方阶段进度。
            </p>
          </section>
        )}
        {tab === 4 && !result && execution && (
          <section className={styles.panel}>
            <h2>证据与结论</h2>
            <p>
              当前可查看已完成节点的局部执行与备用证据。整体安全链复验、经济性和最终评价将在完整结果生成后展示。
            </p>
          </section>
        )}
        <section className={styles.panel} hidden={tab !== 4}>
          <h2>两种初始工况，使用同一套算法</h2>
          <p>
            分别计算或载入两种记录后，可对照查看。下表只引用当前载入的结果；比较用于说明初始电量对执行路径的影响。
          </p>
          <div className={styles.tableWrap}>
            <table>
              <thead>
                <tr>
                  <th>工况</th>
                  <th>完成区间</th>
                  <th>经济 / 后备</th>
                  <th>窗口采纳</th>
                  <th>三地区初始 → 末端电量 / MWh</th>
                  <th>总体状态</th>
                </tr>
              </thead>
              <tbody>
                {(['normal', 'low_energy'] as Scenario[]).map((s) => {
                  const r = records[s]?.result,
                    m = r ? metrics(r) : null;
                  return (
                    <tr key={s}>
                      <td>{scenarioLabels[s]}</td>
                      <td>{m?.completed ?? '未载入'}</td>
                      <td>{m ? `${m.economic} / ${m.backup}` : '—'}</td>
                      <td>{m?.adopted ?? '—'}</td>
                      <td>
                        {r?.patent2_evidence?.inputs.regions.map(
                          (region, i) => (
                            <div key={region.name}>
                              {labels[region.name]}{' '}
                              {number(region.initial_energy_mwh)} →{' '}
                              {number(
                                states(
                                  r.cluster_execution!,
                                  metrics(r).completed,
                                )[i]?.energy_mwh,
                              )}
                            </div>
                          ),
                        ) ?? '—'}
                      </td>
                      <td>{m ? statusLabels[m.status] : '—'}</td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        </section>
        {inputs && (
          <details className={styles.panel}>
            <summary>输入来源、版本与可追溯记录</summary>
            <p>
              共同条件来自所选工况的输入快照；运行结果来自现场计算或载入的真实运行记录。求解器版本、计算预算或运行环境变化可能使不同批次的执行轨迹有所不同。
            </p>
            <p>
              算例版本：{codedRegionText(inputs.version ?? '')}
              <br />
              输入摘要：<code>{inputs.input_sha256}</code>
              <br />
              源码文件摘要（
              {result?.patent2_evidence?.code_snapshot_phase === 'before_run'
                ? '运行前采集'
                : '归档时采集'}
              ）：
              <code>
                {result?.patent2_evidence?.code_sha256 ?? '完成运行后记录'}
              </code>
            </p>
            <button
              onClick={() =>
                download(
                  inputs.input_snapshot_json,
                  `patent2-${scenario}-case.json`,
                )
              }
            >
              下载原始输入快照
            </button>
            <p className={styles.muted}>
              输入快照用于审阅和复算，不是完整回放文件。完整回放请使用页面顶部“导出完整记录”。
            </p>
          </details>
        )}
      </main>
    </div>
  );
}
