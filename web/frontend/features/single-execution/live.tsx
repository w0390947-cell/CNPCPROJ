'use client';

import { useEffect, useRef, useState, type ReactNode } from 'react';
import type { JobStatus, SimulationResult } from '../../lib/simulation-api';
import type { TubeState } from '../../shared/api/generated/single-execution';
import type {
  LiveInitial as Initial,
  LiveCycle as Cycle,
} from '../../shared/api/simulation-live';
import { getSimulationLive } from '@/lib/simulation-api';
import { EvidenceChart } from './chart';
import { clock, value, interval, kindLabels } from './presentation';
import { NetworkTopology } from './network-topology';
import { SingleExecutionDemo } from './demo';
import workspace from '../../shared/ui/scientific-workspace.module.css';
import styles from './live.module.css';
import exampleStyles from '../patent2/style.module.css';
import { ExampleConditions, ExampleConclusion } from './example';
import { SingleInputCurves } from './input-curves';
import type { SingleExampleInputs } from '../../shared/api/generated/single-example';

type LiveState = {
  id: string;
  initial?: Initial;
  cycles: Cycle[];
  message: string;
  error: string;
};

/** Adapt completed evidence to the same view model as committed live events. */
export function completedExecution(
  id: string,
  result: SimulationResult,
): LiveState | null {
  const u = result.single_execution?.unified;
  if (!u) return null;
  const initial: Initial = {
    input_record: result.single_execution?.input_record,
    input_forecast: result.single_execution?.input_forecast,
    domain: u.domain,
    update_steps: u.update_steps,
    kind: 'initial',
    sequence: 0,
    total: u.requested_steps,
    initial: u.initial,
    devices: u.domain.devices,
    network: u.domain.network,
    energy_task: u.domain.energy_task,
    p_band_mw: u.domain.p_band_mw,
    q_band_mvar: u.domain.q_band_mvar,
  };
  const cycles: Cycle[] = (u.steps ?? []).map((row, i) => ({
    kind: 'cycle',
    sequence: i + 1,
    minute: row.step + 1,
    row,
    voltage: row.cycle_evidence?.endpoint_network.voltage_bounds ?? null,
    network_certified: row.cycle_evidence?.endpoint_network.certified ?? null,
    tracking_until:
      u.plans?.[row.plan_index]?.tracking?.guaranteed_until_step ?? null,
    message: '设备执行与反馈',
  }));
  return { id, initial, cycles, message: '计算完成', error: '' };
}

export function useLiveExecution(status: JobStatus | null) {
  const [state, setState] = useState<LiveState | null>(null);
  const terminal = useRef(false);
  const jobState = status?.state;
  useEffect(() => {
    terminal.current = !!jobState && !['queued', 'running'].includes(jobState);
  }, [jobState]);
  const id = status?.simulation_id;
  useEffect(() => {
    if (!id) return;
    let alive = true;
    let timer: ReturnType<typeof setTimeout>;
    let cursor = 0;
    let stream: string | null = null;
    let current: LiveState = {
      id,
      cycles: [],
      message: '等待执行数据',
      error: '',
    };
    async function poll() {
      try {
        const batch = await getSimulationLive(id!, cursor, stream);
        if (!alive) return;
        if (batch.stream !== stream) {
          cursor = 0;
          current = { id: id!, cycles: [], message: '等待执行数据', error: '' };
        }
        for (const event of batch.events) {
          if (event.sequence !== cursor + 1)
            throw new Error('实时记录序号不连续');
          if (event.kind === 'initial') current.initial = event;
          else if (event.kind === 'cycle') {
            if (
              !current.initial ||
              event.minute !== current.cycles.length + 1 ||
              event.row.after.p_mw.length !==
                current.initial.devices.resources.length
            )
              throw new Error('实时执行记录不连续');
            current.cycles = [...current.cycles, event];
            current.message = event.message;
          } else current.message = event.message;
          cursor = event.sequence;
        }
        stream = batch.stream;
        current.error = '';
        setState({ ...current });
        if (batch.more || !terminal.current)
          timer = setTimeout(poll, batch.more ? 0 : 400);
      } catch {
        if (!alive) return;
        setState({
          ...current,
          error: '实时连接中断，正在尝试恢复；已收到的数据保留',
        });
        if (!terminal.current) timer = setTimeout(poll, 1500);
      }
    }
    void poll();
    return () => {
      alive = false;
      clearTimeout(timer);
    };
  }, [id]);
  return state?.id === id ? state : null;
}

export function LiveExecution({
  data,
  status,
  sourceLabel,
  generatedAt,
  result,
  summary,
  onPreview,
}: {
  data: LiveState | null;
  status: JobStatus | null;
  sourceLabel?: string;
  generatedAt?: string;
  result?: SimulationResult | null;
  summary?: ReactNode;
  onPreview?: (inputs: SingleExampleInputs) => void;
}) {
  const initial = data?.initial;
  const cycles = data?.cycles ?? [];
  const [selected, setSelected] = useState<number | null>(null);
  const [playing, setPlaying] = useState(false);
  const [resourceId, setResourceId] = useState('aggregate');
  const [view, setView] = useState(
    status && ['queued', 'running'].includes(status.state) ? 2 : 0,
  );
  const cursor = Math.min(selected ?? cycles.length, cycles.length);
  const last = cursor ? cycles[cursor - 1] : undefined;
  const running = !!status && ['queued', 'running'].includes(status.state);
  useEffect(() => {
    if (!playing || selected == null || selected >= cycles.length) return;
    const timer = window.setInterval(
      () => setSelected((minute) => Math.min(cycles.length, (minute ?? 0) + 1)),
      250,
    );
    return () => window.clearInterval(timer);
  }, [playing, selected, cycles.length]);
  const playbackActive =
    playing && selected != null && selected < cycles.length;
  const animated = (running && selected == null) || playbackActive;
  function locate(minute: number) {
    setPlaying(false);
    setSelected(Math.max(0, Math.min(cycles.length, Math.round(minute))));
  }
  const state = last?.row.after ?? initial?.initial;
  function showInputOutcome(nextView: number, target: string) {
    const page = document;
    setView(nextView);
    window.setTimeout(() => page.getElementById(target)?.scrollIntoView?.({ block: 'start' }), 0);
  }
  const inputPlayback = {
    minute: cursor,
    completed: cycles.length,
    rows: cycles.map(c => c.row),
    onSelect: locate,
    onPlanning: () => showInputOutcome(1, 'single-planning'),
    onResponse: () => showInputOutcome(2, 'single-device-response'),
    decision: last ? `本区间采用${{ economic: '经济反馈', retained_feedback: '原目标反馈续接', backup: '备用续接', terminal: '安全终态保持' }[last.row.disposition]}；PCC 目标 P ${value(last.row.requested_p_mw)} MW / Q ${value(last.row.requested_q_mvar)} Mvar。` : undefined,
    response: last ? `${cursor} min 储能电量 ${value(last.row.after.energy_mwh)} MWh；PCC 响应 P ${interval(last.row.pcc_p_mw)} MW / Q ${interval(last.row.pcc_q_mvar)} Mvar。` : undefined,
  };
  const resources = initial?.devices.resources ?? [];
  const sum = (s: TubeState, kind: string, axis: 'p_mw' | 'q_mvar' = 'p_mw') =>
    resources.reduce((v, r, i) => v + (r.kind === kind ? s[axis][i] : 0), 0);
  const mid = (v: { low: number; high: number }) => (v.low + v.high) / 2;
  const colors = [
    'var(--plot-blue, #5fd6ff)',
    'var(--plot-orange, #ffd27b)',
    'var(--plot-green, #70e0b7)',
    'var(--plot-purple, #b29bff)',
  ];
  const minutes = [0, ...cycles.map((c) => c.minute)];
  const net = initial?.network;
  const resourceIndex = resources.findIndex(
    (resource) => resource.resource_id === resourceId,
  );
  const deviceCurves = (axis: 'p_mw' | 'q_mvar') =>
    resourceIndex < 0
      ? (['wind', 'pv', 'storage', 'svg'] as const).map((kind, i) => ({
          label: kindLabels[kind],
          color: colors[i],
          values: [
            sum(initial!.initial, kind, axis),
            ...cycles.map((c) => sum(c.row.after, kind, axis)),
          ],
        }))
      : [
          {
            label: `${resourceId} 已完成`,
            color: colors[0],
            values: [
              initial!.initial[axis][resourceIndex],
              ...cycles.map((c) => c.row.after[axis][resourceIndex]),
            ],
          },
          {
            label: '采用命令',
            color: colors[1],
            dashed: true,
            values: [
              null,
              ...cycles.map((c) => c.row.command[axis][resourceIndex]),
            ],
          },
        ];
  return (
    <section
      className={styles.live}
      aria-label="仿真实时演示"
      data-running={running}
    >
      {result && (
        <>
          <div className={exampleStyles.provenance}>
            <b>SC 单微网</b> · {sourceLabel || '当前运行记录'}
            {generatedAt ? ` · 生成时间 ${generatedAt}` : ''}
            {status ? ` · 任务：${status.simulation_id}` : ''}
          </div>
          {summary}
        </>
      )}
      {!result && status && (
        <section className={exampleStyles.panel} aria-label="实时执行摘要">
          <h2>
            本次仿真 · 已完成 {cycles.length}
            {initial ? ` / ${initial.total}` : ''} 周期
          </h2>
          <p>
            {running
              ? '实时跟踪当前任务。'
              : status.state === 'succeeded'
                ? '执行已结束，正在读取完整结果。'
                : '计算已停止，保留已收到的执行记录。'}
            完整结果生成前，局部校核不代表全部评价通过。
          </p>
          {data?.message && <p>{data.message}</p>}
          {!running && status.error_message && <p>{status.error_message}</p>}
        </section>
      )}
      {data?.error && <output role="alert">{data.error}</output>}
      {state && initial && (
        <section className={`${exampleStyles.panel} ${exampleStyles.player}`}>
          <div className={exampleStyles.controls} aria-label="统一执行记录控制">
            <button
              type="button"
              onClick={() => {
                if (playbackActive) {
                  setPlaying(false);
                  return;
                }
                setSelected(cursor >= cycles.length ? 0 : cursor);
                setPlaying(true);
              }}
              disabled={!cycles.length}
            >
              {playbackActive ? '暂停轨迹播放' : '播放轨迹'}
            </button>
            <button
              type="button"
              onClick={() => locate(cursor - 1)}
              disabled={cursor === 0}
            >
              查看上一周期
            </button>
            <button
              type="button"
              onClick={() => locate(cursor + 1)}
              disabled={cursor >= cycles.length}
            >
              查看下一周期
            </button>
            <button
              type="button"
              onClick={() => {
                setPlaying(false);
                setSelected(null);
              }}
              disabled={selected == null}
            >
              跟随最新周期
            </button>
            <strong aria-label="当前回放周期">
              当前位置 {clock(cursor)} · {cursor} / {initial.total} 周期
            </strong>
          </div>
          <input
            className={exampleStyles.slider}
            type="range"
            aria-label="选择执行记录周期"
            min={0}
            max={cycles.length}
            value={cursor}
            onChange={(event) => locate(Number(event.target.value))}
          />
        </section>
      )}
      <nav className={workspace.tabs} aria-label="实施例讲解步骤">
        {['共同条件', '规划决策', '执行监控', '专项任务', '证据与结论'].map(
          (label, index) => (
            <button
              key={label}
              type="button"
              aria-current={view === index ? 'step' : undefined}
              onClick={() => setView(index)}
            >
              <span>0{index + 1}</span>
              {label}
            </button>
          ),
        )}
      </nav>
      <div hidden={view !== 0}>
        <ExampleConditions
          onPreview={onPreview}
          result={result}
          current={
            initial?.domain && initial.update_steps
              ? {
                  domain: initial.domain,
                  initial: initial.initial,
                  total: initial.total,
                  update_steps: initial.update_steps,
                  input_forecast: initial.input_forecast,
                }
              : undefined
          }
          inputPlayback={inputPlayback}
        />
      </div>
      {view === 2 && initial?.domain && (
        <SingleInputCurves domain={initial.domain} total={initial.total} forecast={initial.input_forecast} {...inputPlayback} />
      )}
      <div hidden={view !== 4}>
        <ExampleConclusion result={result} />
      </div>
      {!state || !initial ? (
        <section
          className={exampleStyles.panel}
          hidden={view === 0 || view === 4}
        >
          <p>
            当前尚无已完成执行记录。运行期间将显示真实提交的周期节点；规划求解期间请查看上方阶段进度。
          </p>
        </section>
      ) : (
        <>
          <div hidden={view !== 2}>
            <div className={styles.devices} id="single-device-response">
              {(['wind', 'pv', 'storage', 'svg'] as const).map((kind, i) => (
                <div
                  key={kind}
                  className={styles.device}
                  style={{ borderTopColor: colors[i] }}
                >
                  <svg viewBox="0 0 80 64" aria-hidden="true">
                    {kind === 'wind' ? (
                      <>
                        <path d="M40 28V62" />
                        <g
                          className={
                            animated && sum(state, kind) > 0 ? styles.spin : ''
                          }
                        >
                          {[0, 120, 240].map((a) => (
                            <path
                              key={a}
                              transform={`rotate(${a} 40 28)`}
                              d="M40 28L37 3L44 5Z"
                            />
                          ))}
                        </g>
                      </>
                    ) : kind === 'pv' ? (
                      <>
                        <circle
                          cx="65"
                          cy="12"
                          r="7"
                          className={
                            animated && sum(state, kind) > 0 ? styles.pulse : ''
                          }
                        />
                        <path d="M16 22H59L67 50H8ZM15 36H63M32 22L29 50M46 22L49 50M36 50V60" />
                      </>
                    ) : kind === 'storage' ? (
                      <>
                        <rect x="15" y="12" width="47" height="40" rx="4" />
                        <path d="M63 25H70V38H63" />
                        <path
                          className={
                            animated && Math.abs(sum(state, kind)) > 0.001
                              ? styles.pulse
                              : ''
                          }
                          d="M41 18L29 34H43L35 47"
                        />
                      </>
                    ) : (
                      <>
                        <circle cx="40" cy="32" r="24" />
                        <path
                          className={
                            animated &&
                            Math.abs(sum(state, kind, 'q_mvar')) > 0.001
                              ? styles.pulse
                              : ''
                          }
                          d="M18 32Q29 10 40 32T62 32"
                        />
                      </>
                    )}
                  </svg>
                  <h3>{kindLabels[kind]}</h3>
                  <strong>{value(sum(state, kind), 3)} MW</strong>
                  <span>{value(sum(state, kind, 'q_mvar'), 3)} Mvar</span>
                  {kind === 'storage' && (
                    <span>
                      {value(state.energy_mwh, 4)} MWh ·{' '}
                      {sum(state, kind) < -0.001
                        ? '充电'
                        : sum(state, kind) > 0.001
                          ? '放电'
                          : '保持'}
                    </span>
                  )}
                </div>
              ))}
            </div>
          </div>
          <div hidden={view !== 2}>
            <div className={styles.metrics}>
              <span>PCC P：{interval(last?.row.pcc_p_mw, 4)} MW</span>
              <span>PCC Q：{interval(last?.row.pcc_q_mvar, 4)} Mvar</span>
              <span>
                本周期跟踪：
                {!last || last.tracking_until == null
                  ? '未设置承诺'
                  : last.minute < last.row.deadline_step
                    ? '响应过渡中'
                    : last.minute > last.tracking_until
                      ? '保证区间外'
                      : last.row.tracking_certified
                        ? '通过'
                        : '未通过'}
              </span>
              <span>
                本周期网络校核：
                {last?.network_certified == null
                  ? '未记录'
                  : last.network_certified
                    ? '通过'
                    : '未通过'}
              </span>
            </div>
          </div>
          <label hidden={view !== 2}>
            曲线对象{' '}
            <select
              aria-label="选择实际响应资源"
              value={resourceId}
              onChange={(event) => setResourceId(event.target.value)}
            >
              <option value="aggregate">按设备类型汇总</option>
              {resources.map((r) => (
                <option key={r.resource_id} value={r.resource_id}>
                  {r.resource_id}
                </option>
              ))}
            </select>
          </label>
          <div className={styles.charts}>
            <div hidden={view !== 2}>
              <EvidenceChart
                title="设备无功 / Mvar"
                minutes={minutes}
                curves={deviceCurves('q_mvar')}
                selected={cursor}
                onSelect={locate}
              />
              <EvidenceChart
                title="设备有功 / MW"
                minutes={minutes}
                curves={deviceCurves('p_mw')}
                selected={cursor}
                onSelect={locate}
              />
            </div>
            <div hidden={view !== 3}>
              <EvidenceChart
                title="储能电量 / MWh"
                minutes={minutes}
                curves={[
                  {
                    label: '电量',
                    color: colors[2],
                    values: [
                      initial.initial.energy_mwh,
                      ...cycles.map((c) => c.row.after.energy_mwh),
                    ],
                  },
                  {
                    label: '物理下限',
                    color: colors[3],
                    dashed: true,
                    values: minutes.map(() => initial.devices.energy_mwh.low),
                  },
                  {
                    label: '物理上限',
                    color: colors[3],
                    dashed: true,
                    values: minutes.map(() => initial.devices.energy_mwh.high),
                  },
                  ...(initial.energy_task
                    ? [
                        {
                          label: '任务下限（截止时刻要求）',
                          color: colors[1],
                          dashed: true,
                          values: minutes.map(
                            () => initial.energy_task!.energy_mwh.low,
                          ),
                        },
                        {
                          label: '任务上限（截止时刻要求）',
                          color: colors[1],
                          dashed: true,
                          values: minutes.map(
                            () => initial.energy_task!.energy_mwh.high,
                          ),
                        },
                      ]
                    : []),
                ]}
                markers={
                  initial.energy_task
                    ? [
                        {
                          minute: initial.energy_task.deadline_step,
                          label: '原任务截止 T',
                        },
                      ]
                    : undefined
                }
                selected={cursor}
                onSelect={locate}
              />
            </div>
            <div hidden={view !== 2}>
              {(['p', 'q'] as const).map((axis) => {
                const band =
                  axis === 'p' ? initial.p_band_mw : initial.q_band_mvar;
                const range = (c: Cycle) =>
                  axis === 'p' ? c.row.pcc_p_mw : c.row.pcc_q_mvar;
                const target = (c: Cycle) =>
                  axis === 'p' ? c.row.requested_p_mw : c.row.requested_q_mvar;
                return (
                  <EvidenceChart
                    key={axis}
                    title={`PCC ${axis.toUpperCase()} 跟踪 / ${axis === 'p' ? 'MW' : 'Mvar'}`}
                    minutes={minutes}
                    curves={[
                      {
                        label: '区间中点',
                        color: colors[0],
                        values: [null, ...cycles.map((c) => mid(range(c)))],
                      },
                      {
                        label: '评价目标',
                        color: colors[1],
                        dashed: true,
                        values: [null, ...cycles.map(target)],
                      },
                      {
                        label: '外包上界',
                        color: colors[3],
                        values: [null, ...cycles.map((c) => range(c).high)],
                      },
                      {
                        label: '外包下界',
                        color: colors[3],
                        values: [null, ...cycles.map((c) => range(c).low)],
                      },
                      ...(band == null
                        ? []
                        : [
                            {
                              label: '误差带上界',
                              color: colors[2],
                              dashed: true,
                              values: [
                                null,
                                ...cycles.map((c) => target(c) + band),
                              ],
                            },
                            {
                              label: '误差带下界',
                              color: colors[2],
                              dashed: true,
                              values: [
                                null,
                                ...cycles.map((c) => target(c) - band),
                              ],
                            },
                          ]),
                    ]}
                    markers={
                      last?.tracking_until == null
                        ? undefined
                        : [
                            {
                              minute: last.row.deadline_step,
                              label: '响应期限 D',
                            },
                            {
                              minute: last.tracking_until,
                              label: '保证终点 G',
                            },
                          ]
                    }
                    selected={cursor}
                    onSelect={locate}
                  />
                );
              })}
            </div>
          </div>
          {view === 2 &&
            (initial.p_band_mw == null || initial.q_band_mvar == null) && (
              <p>
                当前记录未提供 PCC
                误差带，已显示目标及功率区间；完整证据可用后再显示误差带。
              </p>
            )}
          {view === 2 && last && (
            <p>
              当前执行方式：
              {
                {
                  economic: '经济反馈',
                  retained_feedback: '原目标反馈续接',
                  backup: '备用续接',
                  terminal: '安全终态保持',
                }[last.row.disposition]
              }
              ；PCC 目标 P / Q：{value(last.row.requested_p_mw, 4)} MW /{' '}
              {value(last.row.requested_q_mvar, 4)} Mvar；响应期限第{' '}
              {last.row.deadline_step} 周期，保证终点{' '}
              {last.tracking_until ?? '未设置'}。
            </p>
          )}
          {view === 3 && initial.energy_task && (
            <p>
              储能原任务：第 {initial.energy_task.deadline_step} 周期，电量{' '}
              {interval(initial.energy_task.energy_mwh, 4)} MWh；当前{' '}
              {value(state.energy_mwh, 4)} MWh。
              {(last?.minute ?? 0) < initial.energy_task.deadline_step
                ? '尚未到期'
                : '已到原任务期限，最终判定见结果'}
            </p>
          )}
          <div hidden={view !== 2}>
            {net ? (
              <NetworkTopology
                network={net}
                resources={resources}
                state={state}
                voltage={last?.voltage}
                animated={animated && view === 2}
              />
            ) : (
              <p>当前记录未提供网络拓扑。</p>
            )}
          </div>
          <div hidden={view !== 2} className={styles.table}>
            <table>
              <caption>所选周期设备状态</caption>
              <thead>
                <tr>
                  <th>资源</th>
                  <th>有功 MW</th>
                  <th>无功 Mvar</th>
                  <th>采用命令 P / Q</th>
                </tr>
              </thead>
              <tbody>
                {resources.map((r, i) => (
                  <tr key={r.resource_id}>
                    <td>{r.resource_id}</td>
                    <td>{value(state.p_mw[i])}</td>
                    <td>{value(state.q_mvar[i])}</td>
                    <td>
                      {value(last?.row.command.p_mw[i])} /{' '}
                      {value(last?.row.command.q_mvar[i])}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </>
      )}
      <div hidden={![1, 3, 4].includes(view)} id="single-planning">
        <h3>
          {view === 1
            ? '规划决策 · 滚动窗口与目标接纳'
            : view === 3
              ? '原期限电量任务 · 执行证据'
              : '详细执行证据'}
        </h3>
        {result?.single_execution?.unified ? (
          <SingleExecutionDemo
            result={result}
            busy={running}
            dedicated
            section={
              view === 1 ? 'planning' : view === 3 ? 'energy' : undefined
            }
            playback={{ minute: cursor, onSelect: locate }}
          />
        ) : (
          <p>
            {running
              ? '规划、目标接纳及网络校核等完整证据将在结果生成后显示。'
              : '当前任务未提供完整的详细执行证据。'}
          </p>
        )}
      </div>
    </section>
  );
}
