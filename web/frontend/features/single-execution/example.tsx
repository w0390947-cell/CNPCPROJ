'use client';
import { useEffect, useState } from 'react';
import type { SimulationResult } from '@/lib/simulation-api';
import type { SingleExampleInputs } from '../../shared/api/generated/single-example';
import schema from '../../shared/api/generated/single-example.schema.json';
import { validate, type Schema } from '../../shared/api/schema';
import { codedRegionDisplay } from '../../shared/lib/coded-region-display';
import { NetworkTopology } from './network-topology';
import { interval, value, kindLabels } from './presentation';
import styles from '../patent2/style.module.css';
import { SingleInputCurves, type SingleInputPlayback } from './input-curves';

export function ExampleConditions({
  result,
  current,
  inputPlayback,
  onPreview,
}: {
  result?: SimulationResult | null;
  current?: Pick<
    SingleExampleInputs,
    'domain' | 'initial' | 'total' | 'update_steps' | 'input_forecast'
  >;
  inputPlayback?: SingleInputPlayback;
  onPreview?: (inputs: SingleExampleInputs) => void;
}) {
  const [preview, setPreview] = useState<SingleExampleInputs | null>(null);
  const [error, setError] = useState('');
  const recorded = result?.single_execution?.unified;
  const currentDomain = current?.domain;
  useEffect(() => {
    if (result || currentDomain) return;
    let alive = true;
    const controller = new AbortController();
    fetch(
      `${process.env.NEXT_PUBLIC_API_BASE_URL ?? 'http://127.0.0.1:8000'}/api/single-example/inputs`,
      { signal: controller.signal },
    )
      .then(async (r) => {
        if (!r.ok) throw new Error(`条件读取失败 (${r.status})`);
        const data: unknown = await r.json();
        validate(data, schema as Schema);
        if (alive) {
          setPreview(codedRegionDisplay(data as SingleExampleInputs));
          onPreview?.(data as SingleExampleInputs);
        }
      })
      .catch((e) => {
        if (alive)
          setError(
            codedRegionDisplay(e instanceof Error ? e.message : String(e)),
          );
      });
    return () => {
      alive = false;
      controller.abort();
    };
  }, [result, currentDomain, onPreview]);
  const domain =
    recorded?.domain ??
    current?.domain ??
    (!result ? preview?.domain : undefined);
  const initial =
    recorded?.initial ??
    current?.initial ??
    (!result ? preview?.initial : undefined);
  if (!domain || !initial)
    return (
      <section className={styles.panel}>
        <h2>共同条件</h2>
        <p>
          {result
            ? '该历史记录未保存完整执行域，不能用当前配置补充历史条件。'
            : error || '正在读取正式模型的固定条件，不启动求解。'}
        </p>
      </section>
    );
  const total = recorded?.requested_steps ?? current?.total ?? preview!.total;
  const update =
    recorded?.update_steps ?? current?.update_steps ?? preview!.update_steps;
  const task = domain.energy_task;
  return (
    <div className={styles.stack}>
      <section className={styles.panel}>
        <h2>共同条件 · SC 单微网</h2>
        <p>
          {recorded || current
            ? '以下条件取自当前运行记录。'
            : '以下为正式模型准备流程提供的固定条件预览；实际执行条件以运行记录为准。'}
          参考计划覆盖全天，执行与滚动规划采用各自声明的时间尺度。
        </p>
        <div className={styles.facts}>
          <span>
            参考计划
            <b>
              {result?.metadata.steps ??
                preview?.reference_time_hours.length ??
                '8（固定算例）'}{' '}
              个时段
            </b>
          </span>
          <span>
            执行时域 / 步长
            <b>
              {total} min / {domain.devices.step_minutes} min
            </b>
          </span>
          <span>
            滚动间隔 / 响应期限
            <b>
              {update} min / {domain.response_steps} 个周期
            </b>
          </span>
          <span>
            PCC 有功范围
            <b>
              {value(domain.network.pcc_min_mw)}—
              {value(domain.network.pcc_max_mw)} MW
            </b>
          </span>
          <span>
            P / Q 跟踪误差带
            <b>
              ±{value(domain.p_band_mw)} MW / ±{value(domain.q_band_mvar)} Mvar
            </b>
          </span>
          <span>
            电压范围
            <b>
              {value(domain.network.voltage_min_pu)}—
              {value(domain.network.voltage_max_pu)} pu
            </b>
          </span>
          <span>
            储能初始电量 / 物理范围
            <b>
              {value(initial.energy_mwh)} /{' '}
              {interval(domain.devices.energy_mwh)} MWh
            </b>
          </span>
          <span>
            原期限电量任务
            <b>
              {task
                ? `${task.deadline_step} min，${interval(task.energy_mwh)} MWh`
                : '未设置'}
            </b>
          </span>
        </div>
        <p>
          末端参考服务于规划；原期限任务仍按原截止时刻和目标区间判定。输入包络和模型假设是执行保证的适用条件。
        </p>
      </section>
      <SingleInputCurves
        domain={domain}
        total={total}
        forecast={result ? result.single_execution?.input_forecast : current ? current.input_forecast : preview?.input_forecast}
        {...inputPlayback}
      />
      <section className={styles.panel}>
        <h2>网络、设备权限与声明初态</h2>
        <NetworkTopology
          network={domain.network}
          resources={domain.devices.resources}
          state={initial}
          label="单微网共同条件网络"
        />
        <div className={styles.tableWrap}>
          <table>
            <thead>
              <tr>
                <th>设备 / 类型 / 母线</th>
                <th>P 范围 / MW</th>
                <th>Q 范围 / Mvar</th>
                <th>容量 / MVA</th>
                <th>初态 P / Q</th>
                <th>P 响应系数 / 爬坡 MW·min⁻¹</th>
              </tr>
            </thead>
            <tbody>
              {domain.devices.resources.map((r, i) => (
                <tr key={r.resource_id}>
                  <td>
                    {r.resource_id} · {kindLabels[r.kind]} · {r.bus}
                  </td>
                  <td>{interval(r.p)}</td>
                  <td>{interval(r.q)}</td>
                  <td>{value(r.s_mva)}</td>
                  <td>
                    {value(initial.p_mw[i])} / {value(initial.q_mvar[i])}
                  </td>
                  <td>
                    {value(r.alpha_p)} / {value(r.ramp_p_mw)}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
        <details>
          <summary>支路参数与模型假设</summary>
          <div className={styles.tableWrap}>
            <table>
              <thead>
                <tr>
                  <th>支路</th>
                  <th>父节点 → 子节点</th>
                  <th>r / x（模型值）</th>
                  <th>容量 / MVA</th>
                </tr>
              </thead>
              <tbody>
                {domain.network.branches.map((b, i) => (
                  <tr key={i}>
                    <td>B{i + 1}</td>
                    <td>
                      {domain.network.buses[b.parent]} →{' '}
                      {domain.network.buses[b.child]}
                    </td>
                    <td>
                      {value(b.r_pu, 6)} / {value(b.x_pu, 6)}
                    </td>
                    <td>{value(b.rating_mva)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          <p>
            声明负荷相对误差：
            {value((domain.research.load_relative_error ?? 0) * 100, 2)}%；风电
            / 光伏容量扣减：
            {value((domain.research.wind_nameplate_error ?? 0) * 100, 2)}% /{' '}
            {value((domain.research.pv_nameplate_error ?? 0) * 100, 2)}%。
          </p>
          <p>
            预测插值：
            {domain.research.forecast_interpolation === 'linear'
              ? '线性'
              : '保持'}
            ；研究末端之后的保证依赖末个声明预测包络继续有效。
          </p>
        </details>
      </section>
      <section className={styles.panel}>
        <h2>连续时段输入与声明包络</h2>
        <p>
          下表按滚动间隔抽取执行模型的输入节点；负荷以声明区间表示，风光为声明的最低可用功率。
        </p>
        <div className={styles.tableWrap}>
          <table>
            <thead>
              <tr>
                <th>分钟</th>
                <th>负荷 P / MW</th>
                <th>负荷 Q / Mvar</th>
                <th>风电最低可用 / MW</th>
                <th>光伏最低可用 / MW</th>
              </tr>
            </thead>
            <tbody>
              {domain.forecasts.map((f, i) =>
                i % update ? null : (
                  <tr key={i}>
                    <td>{i}</td>
                    <td>
                      {interval({
                        low: f.loads.reduce((s, l) => s + l.p_mw.low, 0),
                        high: f.loads.reduce((s, l) => s + l.p_mw.high, 0),
                      })}
                    </td>
                    <td>
                      {interval({
                        low: f.loads.reduce((s, l) => s + l.q_mvar.low, 0),
                        high: f.loads.reduce((s, l) => s + l.q_mvar.high, 0),
                      })}
                    </td>
                    {(['wind', 'pv'] as const).map((kind) => (
                      <td key={kind}>
                        {value(
                          f.generation.reduce(
                            (sum, g) =>
                              sum +
                              (domain.devices.resources.some(
                                (r) =>
                                  r.resource_id === g.resource_id &&
                                  r.kind === kind,
                              )
                                ? g.minimum_available_mw
                                : 0),
                            0,
                          ),
                        )}
                      </td>
                    ))}
                  </tr>
                ),
              )}
            </tbody>
          </table>
        </div>
      </section>
    </div>
  );
}

const verdict: Record<string, string> = {
  passed: '通过',
  violated: '未通过',
  unknown: '未获完整证明',
  unproven: '未获证明',
  not_computed: '未计算',
  not_assessed: '尚未评价',
  assumption_violated: '输入假设不满足',
  rejected: '存在未接纳目标',
  met: '按期完成',
  missed: '未按期完成',
  pending: '尚未到期',
  maintained: '持续保持',
  not_required: '无此任务',
};
export function ExampleConclusion({
  result,
}: {
  result?: SimulationResult | null;
}) {
  const e = result?.single_execution,
    u = e?.unified;
  return (
    <section className={styles.panel}>
      <h2>证据与结论 · 各项目分别评价</h2>
      {!result ? (
        <p>
          尚无完整结果。运行中的局部校核不能替代最终评价；请在执行监控和专项任务中查看已完成节点。
        </p>
      ) : (
        <>
          <p>
            总体执行状态：<b>{verdict[e?.status ?? 'unknown']}</b>。{e?.reason}
          </p>
          <div className={styles.tableWrap}>
            <table>
              <thead>
                <tr>
                  <th>评价项目</th>
                  <th>记录结论</th>
                  <th>解释与边界</th>
                </tr>
              </thead>
              <tbody>
                <tr>
                  <td>条件安全</td>
                  <td>{verdict[u?.status ?? 'unknown'] ?? u?.status}</td>
                  <td>以声明的模型、输入范围和后台证书为条件。</td>
                </tr>
                <tr>
                  <td>目标接纳</td>
                  <td>
                    {verdict[u?.target_admission_status ?? 'unknown'] ??
                      u?.target_admission_status}
                  </td>
                  <td>原请求是否被接纳，与后续安全续接分开评价。</td>
                </tr>
                <tr>
                  <td>期限后 P/Q 跟踪</td>
                  <td>
                    {verdict[u?.tracking_status ?? 'unknown'] ??
                      u?.tracking_status}
                  </td>
                  <td>按各目标原响应期限和有限保证区间判断。</td>
                </tr>
                <tr>
                  <td>原期限电量任务</td>
                  <td>{verdict[u?.hard_task?.outcome ?? 'unknown']}</td>
                  <td>
                    {u?.hard_task?.reason ||
                      '恢复过程和规划参考不能替代原期限结果。'}
                  </td>
                </tr>
                <tr>
                  <td>计划成本改善</td>
                  <td>
                    {result.executive_summary.economic_improvement_percent ==
                    null
                      ? '未记录'
                      : `${value(result.executive_summary.economic_improvement_percent, 2)}%`}
                  </td>
                  <td>
                    参考规划成本比较，不等同于实际执行费用或全局最优证明。
                  </td>
                </tr>
              </tbody>
            </table>
          </div>
          <p>
            安全续接不自动代表原经济目标跟踪通过；计算结束也不自动代表所有评价项目通过。
          </p>
        </>
      )}
    </section>
  );
}
