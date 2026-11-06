'use client';

import { useEffect, useId, useMemo, useState } from 'react';
import type { SimulationResult } from '../../lib/simulation-api';
import { EvidenceChart, type Curve } from './chart';
import { NetworkEvidence } from './network';
import { PlanningEvidence } from './planning';
import {
  clock,
  displayExecution,
  executionPoints,
  interval,
  kindLabels,
  parseSingleResult,
  sumKind,
  trackingPhase,
  trackingStatistics,
  value,
} from './presentation';
import styles from './style.module.css';

const colors = [
  'var(--plot-blue, #5fd6ff)',
  'var(--plot-orange, #ffd27b)',
  'var(--plot-green, #70e0b7)',
  'var(--plot-purple, #b29bff)',
  'var(--plot-red, #ff9e8e)',
  'var(--plot-slate, #a7c8e0)',
];
const tabs = {
  devices: '设备实际响应',
  pcc: 'PCC 目标跟踪',
  energy: '原期限电量任务',
  planning: '滚动规划与接纳',
  network: '网络与约束证据',
};
type Tab = keyof typeof tabs;

export function SingleExecutionDemo({
  result,
  busy,
  onImport,
  onPreset,
  dedicated = false,
  playback,
  section,
}: {
  result: SimulationResult | null;
  busy: boolean;
  onImport?: (result: SimulationResult, filename: string) => void;
  onPreset?: () => void;
  dedicated?: boolean;
  section?: Tab;
  playback?: { minute: number; onSelect: (minute: number) => void };
}) {
  const id = useId();
  const raw = result?.single_execution?.unified;
  const u = useMemo(() => (raw ? displayExecution(raw) : null), [raw]);
  const [localTab, setTab] = useState<Tab>('devices');
  const tab = section ?? localTab;
  const [minute, setMinute] = useState(0);
  const [playing, setPlaying] = useState(false);
  const [device, setDevice] = useState('aggregate');
  const [error, setError] = useState('');
  const [loading, setLoading] = useState(false);
  const points = useMemo(() => (u ? executionPoints(u) : []), [u]);
  const stats = useMemo(() => (u ? trackingStatistics(u) : null), [u]);
  const max = points.at(-1)?.minute ?? 0;
  const cursor = Math.min(playback?.minute ?? minute, max);
  const point = points[cursor];
  useEffect(() => {
    if (playback || !playing || cursor >= max || busy) return;
    const timer = window.setInterval(
      () => setMinute((m) => Math.min(max, m + 1)),
      250,
    );
    return () => window.clearInterval(timer);
  }, [playing, cursor, max, busy, playback]);
  const active = playing && cursor < max && !busy;
  function locate(m: number) {
    if (!Number.isFinite(m)) return;
    const target = Math.round(Math.max(0, Math.min(max, m)));
    if (playback) {
      playback.onSelect(target);
      return;
    }
    setPlaying(false);
    setMinute(target);
  }
  const actions = (
    <div className={styles.toolbar}>
      <button type="button" disabled={busy || loading} onClick={onPreset}>
        使用 8.2 核心实施例配置
      </button>
      <label htmlFor={`${id}-file`}>打开已有单微网结果 JSON</label>
      <input
        className={styles.file}
        id={`${id}-file`}
        type="file"
        accept=".json,application/json"
        disabled={busy || loading}
        onChange={async (event) => {
          const file = event.target.files?.[0];
          event.target.value = '';
          if (!file) return;
          setError('');
          setLoading(true);
          setPlaying(false);
          try {
            if (file.size > 100 * 1024 * 1024)
              throw new Error('结果文件超过 100 MB');
            const imported = parseSingleResult(await file.text());
            onImport?.(imported, file.name);
          } catch (e) {
            setError(e instanceof Error ? e.message : '无法读取结果');
          } finally {
            setLoading(false);
          }
        }}
      />
      {loading && <output>正在校验结果文件…</output>}
    </div>
  );
  if (!u || !point || !stats)
    return (
      <section className={styles.demo} aria-label="单微网核心实施例执行演示">
        <h2>8.2 核心实施例 · 执行演示</h2>
        {!dedicated && actions}
        {!dedicated && error && (
          <p role="alert" className={styles.error}>
            {error}
          </p>
        )}
        {!dedicated && (
          <p>
            使用 SC、8
            个全天参考计划时段、启用储能的配置运行，或打开已有真实结果。打开文件只在本机浏览器读取，不提交求解。
          </p>
        )}
        <p>
          当前任务
          {result?.single_execution
            ? '未提供统一执行记录；参考规划未通过时不会启动执行。'
            : '尚无执行结果。'}
          完整执行证据生成后，可逐分钟查看设备、PCC、电量、规划和网络。
        </p>
      </section>
    );

  const resources = u.domain.devices.resources;
  const row = point.row;
  const state = point.state;
  const proposal = [...u.economic_proposals]
    .reverse()
    .find((p) => p.start_step <= (row?.step ?? 0));
  const tracking = row ? u.plans[row.plan_index]?.tracking : undefined;
  const minutes = points.map((p) => p.minute);
  const chart = (
    title: string,
    curves: Curve[],
    bounds?: number[],
    markers?: Array<{ minute: number; label: string }>,
  ) => (
    <EvidenceChart
      title={title}
      minutes={minutes}
      curves={curves}
      bounds={bounds}
      markers={markers}
      selected={cursor}
      onSelect={locate}
    />
  );
  const selectedIndex = resources.findIndex((r) => r.resource_id === device);
  const deviceCurves = (axis: 'p_mw' | 'q_mvar'): Curve[] =>
    selectedIndex >= 0
      ? [
          {
            label: `${device} 已完成${axis === 'p_mw' ? ' P' : ' Q'}`,
            color: colors[0],
            values: points.map((p) => p.state[axis][selectedIndex]),
          },
          {
            label: '最终采用命令',
            color: colors[1],
            dashed: true,
            values: points.map(
              (p) => p.row?.command[axis][selectedIndex] ?? null,
            ),
          },
        ]
      : (['wind', 'pv', 'storage', 'svg'] as const).map((kind, i) => ({
          label: kindLabels[kind],
          color: colors[i],
          values: points.map((p) => sumKind(u, p.state, kind, axis)),
        }));
  const keyMinutes = [
    0, 10, 15, 16, 25, 465, 568, 690, 1080, 1425, 1435, 1440,
  ].filter((m) => m <= max);
  if (!keyMinutes.includes(max)) keyMinutes.push(max);
  return (
    <section className={styles.demo} aria-label="单微网核心实施例执行演示">
      {!playback && (
        <div className={styles.header}>
          <h2>
            {result?.metadata.region === 'SC' &&
            u.domain.network.buses.length === 10
              ? '8.2 核心实施例 · 执行演示'
              : '单微网 · 执行演示'}
          </h2>
          <strong>
            已完成 {max} / {u.requested_steps} 周期
          </strong>
        </div>
      )}
      {!dedicated && actions}
      {!dedicated && error && (
        <p role="alert" className={styles.error}>
          {error}
        </p>
      )}
      {!playback && (
        <div className={styles.toolbar} aria-label="实际执行回放控制">
          <button
            disabled={!max}
            onClick={() => {
              if (cursor >= max) setMinute(0);
              setPlaying(!active);
            }}
          >
            {active ? '暂停执行回放' : '播放执行回放'}
          </button>
          <button disabled={!cursor} onClick={() => locate(cursor - 1)}>
            上一周期
          </button>
          <button disabled={cursor >= max} onClick={() => locate(cursor + 1)}>
            下一周期
          </button>
          <input
            aria-label="实际执行完成分钟"
            type="range"
            min={0}
            max={max}
            step={1}
            value={cursor}
            onInput={(e) => locate(Number(e.currentTarget.value))}
            onChange={(e) => locate(Number(e.target.value))}
          />
          <label>
            完成分钟{' '}
            <input
              aria-label="定位实际完成分钟"
              type="number"
              min={0}
              max={max}
              step={1}
              value={cursor}
              onInput={(e) => locate(Number(e.currentTarget.value))}
              onChange={(e) => locate(Number(e.target.value))}
            />
          </label>
          <strong>
            {clock(cursor)} · 第 {cursor} 周期末 · 原生记录
          </strong>
        </div>
      )}
      {!playback && (
        <div className={styles.stats}>
          <div>
            <span>当前电量 / MWh</span>
            <strong>{value(state.energy_mwh)}</strong>
          </div>
          <div>
            <span>PCC P / MW · 区间中点</span>
            <strong>{value(point.p)}</strong>
          </div>
          <div>
            <span>PCC Q / Mvar · 区间中点</span>
            <strong>{value(point.q)}</strong>
          </div>
          <div>
            <span>本周期评价</span>
            <strong>{trackingPhase(u, row)}</strong>
          </div>
        </div>
      )}
      <fieldset hidden={!!section} className={styles.tabs}>
        <legend>执行证据视图</legend>
        {Object.entries(tabs).map(([key, label]) => (
          <button
            key={key}
            aria-pressed={tab === key}
            onClick={() => setTab(key as Tab)}
          >
            {label}
          </button>
        ))}
      </fieldset>

      {tab === 'devices' && (
        <div>
          {!playback && (
            <>
              <label>
                曲线对象{' '}
                <select
                  aria-label="选择实际响应资源"
                  value={device}
                  onChange={(e) => setDevice(e.target.value)}
                >
                  <option value="aggregate">按设备类型汇总</option>
                  {resources.map((r) => (
                    <option key={r.resource_id} value={r.resource_id}>
                      {r.resource_id}
                    </option>
                  ))}
                </select>
              </label>
              {chart('设备实际有功与采用命令 · MW', deviceCurves('p_mw'))}
              {chart('设备实际无功与采用命令 · Mvar', deviceCurves('q_mvar'))}
            </>
          )}
          <div className={styles.scroll}>
            <table>
              <caption>第 {cursor} 周期设备实际状态与动作额度</caption>
              <thead>
                <tr>
                  <th>资源 / 母线</th>
                  <th>完成 P / MW</th>
                  <th>完成 Q / Mvar</th>
                  <th>命令 P / Q</th>
                  <th>|ΔP| / 本机额度</th>
                  <th>|ΔQ|</th>
                  <th>额定 S / MVA</th>
                  <th>物理 P / Q</th>
                  <th>观测可用 / 保证可用 P</th>
                </tr>
              </thead>
              <tbody>
                {resources.map((r, i) => {
                  const f =
                    u.domain.forecasts[
                      Math.min(row?.step ?? 0, u.domain.forecasts.length - 1)
                    ];
                  return (
                    <tr key={r.resource_id}>
                      <th scope="row">
                        {r.resource_id} / {r.bus}
                      </th>
                      <td>{value(state.p_mw[i])}</td>
                      <td>{value(state.q_mvar[i])}</td>
                      <td>
                        {value(row?.command.p_mw[i])} /{' '}
                        {value(row?.command.q_mvar[i])}
                      </td>
                      <td>
                        {row
                          ? value(
                              Math.abs(row.after.p_mw[i] - row.before.p_mw[i]),
                            )
                          : '—'}{' '}
                        / {r.ramp_p_mw}
                      </td>
                      <td>
                        {row
                          ? value(
                              Math.abs(
                                row.after.q_mvar[i] - row.before.q_mvar[i],
                              ),
                            )
                          : '—'}
                      </td>
                      <td>{r.s_mva}</td>
                      <td>
                        {interval(r.p)} / {interval(r.q)}
                        {r.q_over_p != null ? `；|Q|≤${r.q_over_p}P` : ''}
                      </td>
                      <td>
                        {value(
                          row?.observed.generation.find(
                            (g) => g.resource_id === r.resource_id,
                          )?.minimum_available_mw,
                        )}{' '}
                        /{' '}
                        {value(
                          f?.generation.find(
                            (g) => g.resource_id === r.resource_id,
                          )?.minimum_available_mw,
                        )}
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
          <p>
            本周期共享无功绝对变化合计{' '}
            {row
              ? value(
                  row.after.q_mvar.reduce(
                    (s, q, i) => s + Math.abs(q - row.before.q_mvar[i]),
                    0,
                  ),
                )
              : '未执行'}{' '}
            Mvar；声明额度 {u.domain.devices.aggregate_q_ramp_mvar}{' '}
            Mvar。按绝对变化求和，正负不抵消；页面显示记录差值，不新增安全判定。
          </p>
          <details open>
            <summary>关键设备状态 · 对应表 8-5 的展示口径</summary>
            <div className={styles.scroll}>
              <table>
                <thead>
                  <tr>
                    <th>完成周期</th>
                    <th>风电 P</th>
                    <th>光伏 P</th>
                    <th>PCS P</th>
                    <th>风电 Q</th>
                    <th>SVG Q</th>
                    <th>电量 / MWh</th>
                  </tr>
                </thead>
                <tbody>
                  {keyMinutes.map((m) => {
                    const p = points[m];
                    return (
                      <tr key={m}>
                        <th scope="row">
                          <button onClick={() => locate(m)}>{m}</button>
                        </th>
                        <td>{value(sumKind(u, p.state, 'wind'))}</td>
                        <td>{value(sumKind(u, p.state, 'pv'))}</td>
                        <td>{value(sumKind(u, p.state, 'storage'))}</td>
                        <td>{value(sumKind(u, p.state, 'wind', 'q_mvar'))}</td>
                        <td>{value(sumKind(u, p.state, 'svg', 'q_mvar'))}</td>
                        <td>{value(p.state.energy_mwh)}</td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </div>
            <p>P 单位 MW，Q 单位 Mvar。</p>
          </details>
        </div>
      )}

      {tab === 'pcc' && (
        <div>
          <div className={styles.stats}>
            <div>
              <span>原期限到期评价</span>
              <strong>
                {stats.passed} / {stats.count} 通过
              </strong>
            </div>
            <div>
              <span>最大 P 误差外包界 / MW</span>
              <strong>{value(stats.maxP, 10)}</strong>
            </div>
            <div>
              <span>最大 Q 误差外包界 / Mvar</span>
              <strong>{value(stats.maxQ, 10)}</strong>
            </div>
          </div>
          <p>
            统计沿用后台原响应期限：逐一纳入完成分钟 ≥
            记录期限的周期，不删除未通过记录。保证终点后的结果仍保留，并单独标明；它不自动延长跟踪保证。
          </p>
          {!playback &&
            (['p', 'q'] as const).map((axis) => {
              const target = axis === 'p' ? 'targetP' : 'targetQ';
              const band =
                axis === 'p' ? u.domain.p_band_mw : u.domain.q_band_mvar;
              return (
                <div key={axis}>
                  {chart(
                    `PCC ${axis.toUpperCase()} 实际外包与当前评价目标 · ${axis === 'p' ? 'MW' : 'Mvar'}`,
                    [
                      {
                        label: '周期末区间中点',
                        color: colors[0],
                        values: points.map((p) => p[axis]),
                      },
                      {
                        label: '当前评价目标',
                        color: colors[1],
                        dashed: true,
                        values: points.map((p) => p[target]),
                      },
                      {
                        label: '误差带上界',
                        color: colors[2],
                        dashed: true,
                        values: points.map((p) =>
                          p[target] == null ? null : p[target]! + band,
                        ),
                      },
                      {
                        label: '误差带下界',
                        color: colors[2],
                        dashed: true,
                        values: points.map((p) =>
                          p[target] == null ? null : p[target]! - band,
                        ),
                      },
                      {
                        label: '实际外包上界',
                        color: colors[3],
                        values: points.map(
                          (p) =>
                            p.row?.[axis === 'p' ? 'pcc_p_mw' : 'pcc_q_mvar']
                              .high ?? null,
                        ),
                      },
                      {
                        label: '实际外包下界',
                        color: colors[3],
                        values: points.map(
                          (p) =>
                            p.row?.[axis === 'p' ? 'pcc_p_mw' : 'pcc_q_mvar']
                              .low ?? null,
                        ),
                      },
                    ],
                    undefined,
                    tracking
                      ? [
                          {
                            minute: tracking.deadline_step,
                            label: '响应期限 D',
                          },
                          {
                            minute: tracking.guaranteed_until_step,
                            label: '保证终点 G',
                          },
                        ]
                      : undefined,
                  )}
                </div>
              );
            })}
          <p>
            当前周期：D={row?.deadline_step ?? '未记录'}，采用证书的 G=
            {tracking?.guaranteed_until_step ?? '未承诺'}；
            {trackingPhase(u, row)}。P / Q 误差外包界 {value(point.pError)} MW /{' '}
            {value(point.qError)} Mvar；标准 ±{u.domain.p_band_mw} MW / ±
            {u.domain.q_band_mvar} Mvar。
          </p>
          <div className={styles.scroll}>
            <table>
              <caption>逐周期 PCC 跟踪明细 · 对应表 8-6 的展示口径</caption>
              <thead>
                <tr>
                  <th>周期</th>
                  <th>评价目标 P / Q</th>
                  <th>实际 PCC P / Q 外包</th>
                  <th>误差外包界 P / Q</th>
                  <th>D / G</th>
                  <th>后台结果与阶段</th>
                </tr>
              </thead>
              <tbody>
                {points
                  .filter((p) => p.row)
                  .map((p) => {
                    const r = p.row!;
                    const t = u.plans[r.plan_index]?.tracking;
                    return (
                      <tr key={p.minute}>
                        <th scope="row">
                          <button onClick={() => locate(p.minute)}>
                            {p.minute}
                          </button>
                        </th>
                        <td>
                          {value(p.targetP)} / {value(p.targetQ)}
                        </td>
                        <td>
                          {interval(r.pcc_p_mw)} / {interval(r.pcc_q_mvar)}
                        </td>
                        <td>
                          {value(p.pError)} / {value(p.qError)}
                        </td>
                        <td>
                          {r.deadline_step} /{' '}
                          {t?.guaranteed_until_step ?? '未承诺'}
                        </td>
                        <td>{trackingPhase(u, r)}</td>
                      </tr>
                    );
                  })}
              </tbody>
            </table>
          </div>
          <p>
            实际区间来自后台 AC
            外包；跟踪按区间端点最大绝对误差评价。图中连线不表示周期内连续跟踪已获证明。
          </p>
        </div>
      )}

      {tab === 'energy' && (
        <div>
          {!playback &&
            chart(
              '储能已完成电量与原始任务 · MWh',
              [
                {
                  label: '实际电量',
                  color: colors[0],
                  values: points.map((p) => p.state.energy_mwh),
                },
                {
                  label: '物理下限',
                  color: colors[4],
                  dashed: true,
                  values: points.map(() => u.domain.devices.energy_mwh.low),
                },
                {
                  label: '物理上限',
                  color: colors[4],
                  dashed: true,
                  values: points.map(() => u.domain.devices.energy_mwh.high),
                },
                ...(u.domain.energy_task
                  ? [
                      {
                        label: '原任务下限（截止时刻要求）',
                        color: colors[2],
                        dashed: true,
                        values: points.map(
                          () => u.domain.energy_task!.energy_mwh.low,
                        ),
                      },
                      {
                        label: '原任务上限（截止时刻要求）',
                        color: colors[2],
                        dashed: true,
                        values: points.map(
                          () => u.domain.energy_task!.energy_mwh.high,
                        ),
                      },
                    ]
                  : []),
              ],
              undefined,
              u.domain.energy_task
                ? [
                    {
                      minute: u.domain.energy_task.deadline_step,
                      label: '原任务截止 T',
                    },
                  ]
                : undefined,
            )}
          <p>
            原截止周期 T={u.domain.energy_task?.deadline_step ?? '本次无此任务'}
            ；任务区间 {interval(u.domain.energy_task?.energy_mwh)}{' '}
            MWh。任务区间约束原截止时刻，中间周期仍按物理边界及原期限可完成性校核。
          </p>
          <p>
            声明初始电量 {value(u.initial.energy_mwh)}，当前{' '}
            {value(state.energy_mwh)}，已执行末电量{' '}
            {value(points.at(-1)?.state.energy_mwh)} MWh；到期电量{' '}
            {value(
              u.domain.energy_task
                ? points[u.domain.energy_task.deadline_step]?.state.energy_mwh
                : undefined,
            )}{' '}
            MWh。实际最低 / 最高{' '}
            {value(Math.min(...points.map((p) => p.state.energy_mwh)))} /{' '}
            {value(Math.max(...points.map((p) => p.state.energy_mwh)))} MWh。
          </p>
          <p>
            后台任务保证：{u.hard_task?.guarantee}；到期结果：
            {u.hard_task?.outcome}。{u.hard_task?.reason}
          </p>
          <div className={styles.scroll}>
            <table>
              <caption>本周期 PCS 指令与电量更新</caption>
              <thead>
                <tr>
                  <th>周期前 / 后电量</th>
                  <th>采用 PCS P / MW</th>
                  <th>充电 / 放电效率</th>
                  <th>证书索引 / 执行位置</th>
                </tr>
              </thead>
              <tbody>
                <tr>
                  <td>
                    {value(row?.before.energy_mwh)} / {value(state.energy_mwh)}
                  </td>
                  <td>
                    {row
                      ? value(
                          sumKind(
                            u,
                            { ...state, p_mw: row.command.p_mw },
                            'storage',
                          ),
                        )
                      : '未执行'}
                  </td>
                  <td>
                    {u.domain.devices.eta_charge} /{' '}
                    {u.domain.devices.eta_discharge}
                  </td>
                  <td>
                    {row?.plan_index ?? '—'} / {row?.plan_offset ?? '—'}
                  </td>
                </tr>
              </tbody>
            </table>
          </div>
          <p>
            电量显示执行器已提交的记录；播放、试算查看和成本比较均不推进设备状态或再次积分。
          </p>
        </div>
      )}
      {tab === 'planning' && (
        <PlanningEvidence u={u} proposal={proposal} onSelect={locate} />
      )}
      {tab === 'network' && (
        <NetworkEvidence
          u={u}
          row={row}
          state={state}
          showTopology={!playback}
        />
      )}
    </section>
  );
}
