import type { SimulationResult } from '@/lib/simulation-api';
import type {
  Interval,
} from '@/shared/api/generated/cluster-execution';
import { ClusterExecutionPanel } from '@/features/cluster-coordination';
import { Chart } from './chart';
import {
  keyMinutes,
  labels,
  metrics,
  number,
  reason,
  recoveryTasks,
  reserveNodes,
  sourceLabels,
  states,
  statusLabels,
} from './model';
import type { ExecutionView } from './model';
import { NetworkTopology } from '../single-execution/network-topology';
import styles from './style.module.css';

export function Badge({ status }: { status?: string | null }) {
  return (
    <span className={styles.badge} data-status={status}>
      {statusLabels[status ?? ''] ?? status ?? '未记录'}
    </span>
  );
}
const colors = [
  'var(--plot-green, #53d8c1)',
  'var(--plot-blue, #82acff)',
  'var(--plot-orange, #f7bc73)',
];
const bounds = (v?: Interval | null, digits = 4) =>
  v ? `${number(v.low, digits)}—${number(v.high, digits)}` : '未记录';

export function Overview({ result }: { result: SimulationResult }) {
  const e = result.cluster_execution!,
    m = metrics(result),
    steps = e.safety?.steps ?? [];
  const certified = steps.filter(
    (s) =>
      (e.safety?.domain.regions.length ?? 0) > 0 &&
      s.path.networks.length === e.safety?.domain.regions.length &&
      s.path.networks.every((n) => n.certified),
  ).length;
  const unknown = reserveNodes(e)
    .filter((c) => c.status === 'unknown')
    .map((c) => c.start_minute);
  const last = reserveNodes(e).at(-1);
  return (
    <section className={styles.panel}>
      <h2>
        执行结果 <Badge status={e.status} />
      </h2>
      <div className={styles.stats}>
        {[
          ['完成执行区间', `${m.completed} / 60`],
          ['经济 / 后备动作', `${m.economic} / ${m.backup}`],
          ['滚动窗口采纳', `${m.adopted} / ${e.rolling_updates?.length ?? 0}`],
          [
            '序列化证据回放',
            m.replayed == null ? '未完成' : `${m.replayed} 个状态`,
          ],
        ].map(([k, v]) => (
          <div key={k}>
            <span>{k}</span>
            <strong>{v}</strong>
          </div>
        ))}
      </div>
      <div className={styles.twoColumns}>
        <div>
          <h3>已经证明的部分</h3>
          <p>
            返回记录中，{certified}{' '}
            个已执行区间的三区域连续网络证书均通过。完整设备、联合受电和功率因数校核见“证据与结论”。
          </p>
          <p>
            回放是从序列化证据重新验证已执行状态，不能替代备用调用资格或全部服务指标的证明。
          </p>
        </div>
        <div>
          <h3>需要区分的部分</h3>
          <p>
            {!e.reserve_observations?.length
              ? '本次没有完整备用观测记录，不能判断各节点调用资格。'
              : unknown.length
                ? `完整备用证书在 ${unknown.join('、')} min 节点暂未获证明。`
                : '返回记录中没有暂未获证明的备用观测节点。'}
            末次记录：
            <Badge status={last?.status} />
          </p>
          <p>
            认证后备动作用于保持可行执行；采用后备时，不能把这段执行解释成经济
            P/Q 目标已经跟踪成功。总体 unknown
            表示证明不完整，并不自动等于发生安全越限。
          </p>
        </div>
      </div>
      {e.safety?.failure && (
        <p className={styles.error}>执行停止原因：{reason(e.safety.failure)}</p>
      )}
      {result.patent2_evidence?.replay_error && (
        <p className={styles.error}>
          回放验证未完成：{result.patent2_evidence.replay_error}
        </p>
      )}
    </section>
  );
}

export function Rolling({
  execution: e,
  history = [],
  minute,
  onSelect,
}: {
  execution: ExecutionView;
  history?: SimulationResult['admm_history'];
  minute: number;
  onSelect: (n: number) => void;
}) {
  const windows = e.rolling_updates ?? [];
  const intervalStart = Math.max(0, minute - 1);
  const window =
    windows.find((w) => intervalStart >= w.start_minute && intervalStart < w.end_minute);
  return (
    <section className={styles.panel} id="cluster-planning">
      <h2>滚动规划：用真实初值更新，保留原恢复期限</h2>
      <ol className={styles.workflow}>
        <li>读取本窗口真实设备状态与电量</li>
        <li>三区域 P/Q 分布式协调</li>
        <li>严格区域模型与联合条件检查</li>
        <li>采纳可用计划，或继续认证后备</li>
        <li>分钟执行后反馈实际状态</li>
      </ol>
      <p>
        研究时域为 60 分钟，规划展望上限为 {e.policy.horizon_minutes ?? '—'}{' '}
        分钟；每次只使用剩余研究时段。窗口“采纳”与“ADMM
        收敛”是不同环节，采纳仍须满足后续执行及安全条件。
      </p>
      <div className={styles.tableWrap}>
        <table>
          <thead>
            <tr>
              <th>执行窗口</th>
              <th>规划末端</th>
              <th>ADMM 迭代数</th>
              <th>计划采纳</th>
              <th>窗口状态</th>
              <th>实际决定原因</th>
            </tr>
          </thead>
          <tbody>
            {windows.map((w) => (
              <tr key={w.start_minute} data-selected={window === w}>
                <td>
                  <button onClick={() => onSelect(Math.min(w.start_minute + 1, e.safety?.steps?.length ?? 0))}>
                    {w.start_minute}—{w.end_minute} min
                  </button>
                </td>
                <td>{w.horizon_end_minute} min</td>
                <td>{w.admm_iterations ?? '未记录'}</td>
                <td>{w.adopted ? '是' : '否'}</td>
                <td>
                  <Badge status={w.status} />
                </td>
                <td>{reason(w.reason)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      {window && (
        <>
          <h3>
            选中窗口 {window.start_minute}—{window.end_minute}{' '}
            min：计划与真实反馈
          </h3>
          <p>所选完成节点 {minute} min；对应执行区间 {intervalStart}→{minute || 1} min 的规划窗口。</p>
          <div className={styles.tableWrap}>
            <table>
              <thead>
                <tr>
                  <th>区域</th>
                  <th>真实初值 / MWh</th>
                  <th>末端参考 / MWh</th>
                  <th>窗口实际末电量 / MWh</th>
                  <th>P 参考序列 / MW</th>
                  <th>Q 参考序列 / Mvar</th>
                </tr>
              </thead>
              <tbody>
                {Object.keys(window.initial_energy_mwh ?? {}).map((r) => (
                  <tr key={r}>
                    <td>{labels[r]}</td>
                    <td>{number(window.initial_energy_mwh?.[r])}</td>
                    <td>{number(window.terminal_target_mwh?.[r])}</td>
                    <td>{number(window.actual_end_energy_mwh?.[r])}</td>
                    <td>
                      {window.p_target_mw?.[r]
                        ?.map((v) => number(v))
                        .join(' / ') ?? '未记录'}
                    </td>
                    <td>
                      {window.q_target_mvar?.[r]
                        ?.map((v) => number(v))
                        .join(' / ') ?? '未记录'}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          <p className={styles.muted}>
            参考序列按 15 分钟排列；未采纳窗口的参考不代表已经下发执行。
          </p>
        </>
      )}
      <details>
        <summary>
          查看初始协调的 ADMM 残差（各滚动窗口仅展示其实际记录的迭代数）
        </summary>
        <div className={styles.tableWrap}>
          <table>
            <thead>
              <tr>
                <th>迭代</th>
                <th>原始残差 / 阈值</th>
                <th>对偶残差 / 阈值</th>
              </tr>
            </thead>
            <tbody>
              {history.map((h) => (
                <tr key={h.iteration}>
                  <td>{h.iteration}</td>
                  <td>
                    {number(h.primal_residual, 6)} /{' '}
                    {number(h.primal_tolerance, 6)}
                  </td>
                  <td>
                    {number(h.dual_residual, 6)} / {number(h.dual_tolerance, 6)}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </details>
    </section>
  );
}

export function Devices({
  execution: e,
  minute,
  onSelect,
}: {
  execution: ExecutionView;
  minute: number;
  onSelect: (n: number) => void;
}) {
  const domain = e.safety?.domain,
    steps = e.safety?.steps ?? [],
    current = states(e, minute);
  const step = steps.find((s) => s.minute + 1 === minute),
    regions = domain?.regions ?? [];
  const measured = e.measurements;
  return (
    <div className={styles.stack} id="cluster-device-response">
      <section className={styles.panel}>
        <h2>分钟执行：指令 → 设备响应 → 电量积分</h2>
        <p>
          当前查看 <b>{minute} min 完成节点</b>。
          {minute === 0
            ? '这是执行前的真实初始状态。'
            : `对应 ${minute - 1}→${minute} min 区间。`}{' '}
          {step && (
            <>
              <Badge status={sourceLabels[step.source]} /> {reason(step.reason)}
            </>
          )}
        </p>
        <Chart
          title="储能实际电量 / MWh"
          minute={minute}
          onSelect={onSelect}
          markers={keyMinutes(e)}
          curves={regions.map((r, i) => ({
            label: labels[r.region],
            color: colors[i],
            values: Array.from(
              { length: steps.length + 1 },
              (_, m) => states(e, m)[i]?.energy_mwh ?? null,
            ),
          }))}
        />
        <Chart
          title="储能实际功率 / MW（正放电、负充电）"
          minute={minute}
          onSelect={onSelect}
          curves={regions.map((r, i) => ({
            label: labels[r.region],
            color: colors[i],
            values: Array.from(
              { length: steps.length + 1 },
              (_, m) =>
                states(e, m)[i]?.p_mw[
                  r.devices.resources.findIndex((d) => d.kind === 'storage')
                ] ?? null,
            ),
          }))}
        />
        <p className={styles.muted}>
          曲线连接实际记录的分钟节点，连线只用于阅读；连续安全结论取自后端区间证书。
        </p>
      </section>
      <section className={styles.panel}>
        <h2>区域 PCC：经济目标与实际响应</h2>
        <p>
          原记录按执行区间起点记时；这里将该区间的响应统一放在完成节点（原时间 +
          1 min），与设备电量对齐。后备区间即使安全，也可能偏离原经济目标。
        </p>
        {measured.map((r) => (
          <details key={r.region}>
            <summary>{labels[r.region]} · 展开 P/Q 跟踪曲线</summary>
            {(['p', 'q'] as const).map((axis) => {
              const values = r[axis === 'p' ? 'p_mw' : 'q_mvar'] ?? [],
                targets =
                  r[axis === 'p' ? 'p_target_mw' : 'q_target_mvar'] ?? [];
              const series = (v: (number | null)[]) =>
                Array.from({ length: steps.length + 1 }, (_, m) => {
                  const i = r.time_minutes?.findIndex((t) => t + 1 === m) ?? -1;
                  return i < 0 ? null : (v[i] ?? null);
                });
              return (
                <Chart
                  key={axis}
                  title={`${labels[r.region]} · ${axis.toUpperCase()} / ${axis === 'p' ? 'MW' : 'Mvar'}`}
                  minute={minute}
                  onSelect={onSelect}
                  curves={[
                    {
                      label: '实际响应',
                      color: 'var(--plot-green, #53d8c1)',
                      values: series(values),
                    },
                    {
                      label: '原经济目标',
                      color: 'var(--plot-orange, #f7bc73)',
                      values: series(targets),
                      dashed: true,
                    },
                  ]}
                />
              );
            })}
            <p className={styles.muted}>
              目标缺失时保留空缺，不把后备动作替换成新的经济目标。
            </p>
          </details>
        ))}
      </section>
      {regions.map((r, i) => (
        <section className={styles.panel} key={r.region}>
          <h2>
            {labels[r.region]} <small>完成节点 {minute} min</small>
          </h2>
          <p>
            实际电量 <b>{number(current[i]?.energy_mwh)} MWh</b> · 本区间变化{' '}
            <b>
              {step
                ? number(
                    step.after[i].energy_mwh - step.before[i].energy_mwh,
                    6,
                  )
                : '—'}{' '}
              MWh
            </b>{' '}
            · 边界 {bounds(r.devices.energy_mwh)} MWh
          </p>
          <p>
            充电按 ηc 积分、放电按 1/ηd
            积分；跨零时由后端分段计算。此处显示实际积分结果，不以指令功率代替设备响应。
          </p>
          <div className={styles.tableWrap}>
            <table>
              <thead>
                <tr>
                  <th>设备 / 母线</th>
                  <th>P 指令 / MW</th>
                  <th>P 响应前 → 后 / MW</th>
                  <th>Q 指令 / Mvar</th>
                  <th>Q 响应前 → 后 / Mvar</th>
                  <th>容量 / MVA</th>
                </tr>
              </thead>
              <tbody>
                {r.devices.resources.map((d, j) => (
                  <tr key={d.resource_id}>
                    <td>
                      {d.resource_id} · {d.bus}
                      <small>
                        {
                          {
                            wind: '风电',
                            pv: '光伏',
                            storage: '储能',
                            svg: 'SVG',
                          }[d.kind]
                        }
                      </small>
                    </td>
                    <td>{number(step?.commands[i].p_mw[j])}</td>
                    <td>
                      {step ? `${number(step.before[i].p_mw[j])} → ` : ''}
                      {number(current[i]?.p_mw[j])}
                    </td>
                    <td>{number(step?.commands[i].q_mvar[j])}</td>
                    <td>
                      {step ? `${number(step.before[i].q_mvar[j])} → ` : ''}
                      {number(current[i]?.q_mvar[j])}
                    </td>
                    <td>{number(d.s_mva)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          {step && (
            <p>
              本区间 AC 网络证书{' '}
              <Badge
                status={step.path.networks[i]?.certified ? 'passed' : 'unknown'}
              />{' '}
              · 区域 PCC P 包络 {bounds(step.path.networks[i]?.pcc_p_mw)} MW · Q
              包络 {bounds(step.path.networks[i]?.pcc_q_mvar)} Mvar
            </p>
          )}
          {current[i] && (
            <NetworkTopology
              network={r.network}
              resources={r.devices.resources}
              state={current[i]}
              voltage={step?.path.networks[i]?.voltage_bounds}
              label={`${labels[r.region]}执行网络`}
            />
          )}
          <details>
            <summary>设备模型权限与连续网络边界</summary>
            <div className={styles.tableWrap}>
              <table>
                <thead>
                  <tr>
                    <th>设备</th>
                    <th>P 允许范围 / MW</th>
                    <th>Q 允许范围 / Mvar</th>
                    <th>P 响应系数</th>
                    <th>P 爬坡 / MW·min⁻¹</th>
                  </tr>
                </thead>
                <tbody>
                  {r.devices.resources.map((d) => (
                    <tr key={d.resource_id}>
                      <td>{d.resource_id}</td>
                      <td>{bounds(d.p)}</td>
                      <td>{bounds(d.q)}</td>
                      <td>{number(d.alpha_p)}</td>
                      <td>{number(d.ramp_p_mw)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
              {step && (
                <table>
                  <thead>
                    <tr>
                      <th>母线</th>
                      <th>电压区间 / pu</th>
                    </tr>
                  </thead>
                  <tbody>
                    {r.network.buses.map((b, j) => (
                      <tr key={b}>
                        <td>{b}</td>
                        <td>
                          {bounds(
                            step.path.networks[i]?.voltage_bounds?.[j],
                            6,
                          )}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              )}
            </div>
          </details>
        </section>
      ))}
      {step && (
        <section className={styles.panel}>
          <h3>联合连续受电包络</h3>
          <p>
            P：{bounds(step.path.pcc_p_mw)} MW · Q：
            {bounds(step.path.pcc_q_mvar)} Mvar · 联合受电上限：
            {domain?.import_limit_mw} MW
          </p>
          <p className={styles.muted}>
            包络表示认证范围，并非某一时刻的精确 PCC 测量值。
          </p>
        </section>
      )}
    </div>
  );
}

export function Reserve({
  execution: e,
  final = true,
  minute,
  onSelect,
}: {
  execution: ExecutionView;
  final?: boolean;
  minute: number;
  onSelect: (n: number) => void;
}) {
  const observations = reserveNodes(e),
    at = observations.find((o) => o.start_minute === minute);
  return (
    <section className={styles.panel}>
      <h2>双向备用：基础预留与完整交付证明</h2>
      <p>
        基础功率与能量余量只是必要信息；完整证书还检查响应、持续支撑、区域网络及联合调用条件。电量回到基础范围并不自动代表恢复完成。
      </p>
      <div className={styles.timeline} aria-label="备用证书节点">
        {observations.map((o, i) => (
          <button
            key={`${o.start_minute}-${i}`}
            data-kind={o.status}
            aria-pressed={minute === o.start_minute}
            aria-label={`${o.start_minute} min：${statusLabels[o.status]}`}
            title={`${o.start_minute} min：${reason(o.reason)}`}
            onClick={() => onSelect(o.start_minute)}
          >
            {o.start_minute}
          </button>
        ))}
      </div>
      <p className={styles.muted}>
        绿色：完整证书通过；橙色：暂未获证明。数字是完成节点，点击查看对应证据。同一节点显示最后一次观测。
      </p>
      <h3>
        {minute} min 的完整备用证书 <Badge status={at?.status} />
      </h3>
      <p>
        {at
          ? reason(at.reason)
          : '该完成节点没有备用观测记录，不能推断已通过。'}
      </p>
      {at && (
        <div className={styles.tableWrap}>
          <table>
            <thead>
              <tr>
                <th>区域</th>
                <th>承诺 / MW（上、下方向）</th>
                <th>支撑时间 / min</th>
                <th>交付证书</th>
                <th>原因</th>
              </tr>
            </thead>
            <tbody>
              {at.regions.map((r) => (
                <tr key={r.inputs.region}>
                  <td>{labels[r.inputs.region]}</td>
                  <td>{number(r.inputs.required_mw, 9)}</td>
                  <td>{r.inputs.support_minutes}</td>
                  <td>
                    <Badge status={r.status} />
                  </td>
                  <td>{reason(r.reason)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      <h3>原期限恢复任务</h3>
      <p>
        首次缺额建立任务后保留原截止时间；滚动更新不会把截止时间顺延。
        {final
          ? '以下为本次运行最终归档状态。'
          : '以下为截至最新完成节点的任务状态，尚无最终结论。'}
        可点击已完成的首次发现、截止和关闭节点回看。
      </p>
      <div className={styles.tableWrap}>
        <table aria-label={final ? '恢复任务最终状态' : '恢复任务当前状态'}>
          <thead>
            <tr>
              <th>区域 / 方向</th>
              <th>首次发现</th>
              <th>原截止</th>
              <th>实际关闭</th>
              <th>任务状态</th>
              <th>期限状态</th>
              <th>说明</th>
            </tr>
          </thead>
          <tbody>
            {recoveryTasks(e).map((t, i) => (
              <tr key={i}>
                <td>
                  {labels[t.region]} / {t.direction === 'up' ? '上调' : '下调'}
                </td>
                <td>
                  <button onClick={() => onSelect(t.first_minute)}>
                    {t.first_minute} min
                  </button>
                </td>
                <td>
                  <button onClick={() => onSelect(t.deadline_minute)}>
                    {t.deadline_minute} min
                  </button>
                </td>
                <td>
                  {t.completed_minute == null ? (
                    '尚未关闭'
                  ) : (
                    <button onClick={() => onSelect(t.completed_minute!)}>
                      {t.completed_minute} min
                    </button>
                  )}
                </td>
                <td>
                  <Badge status={t.status} />
                </td>
                <td>
                  <Badge status={t.deadline_status} />
                </td>
                <td>{reason(t.reason)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      {!e.reserve_recovery_history?.length && <p>没有记录恢复任务。</p>}
    </section>
  );
}

export function Evidence({ result }: { result: SimulationResult }) {
  return (
    <div className={styles.stack}>
      {result.reference_economics && (
        <section className={styles.panel}>
          <h2>参考规划的经济性</h2>
          <p>
            下列成本属于规划参考口径，用于解释优化模型的效果；它们不是整段分钟执行的实际结算成本，也不单独证明全部经济性指标通过。
          </p>
          <div className={styles.facts}>
            <span>
              分布式参考经济成本{' '}
              <b>
                {number(
                  result.reference_economics.reference.economic_cost_cny,
                  2,
                )}{' '}
                元
              </b>
            </span>
            <span>
              集中式参考经济成本{' '}
              <b>
                {number(
                  result.reference_economics.centralized_economic_cost_cny,
                  2,
                )}{' '}
                元
              </b>
            </span>
            <span>
              相对参考差异{' '}
              <b>{number(result.reference_economics.gap_percent, 3)} %</b>
            </span>
            <span>
              可比性{' '}
              <b>
                {
                  {
                    comparable: '可比较',
                    not_converged: '协调未收敛',
                    nonpositive_baseline: '参考基数不为正',
                  }[result.reference_economics.comparison_status]
                }
              </b>
            </span>
          </div>
        </section>
      )}
      <ClusterExecutionPanel
        execution={result.cluster_execution}
        quality={result.computation_quality}
        hasResult
        busy={false}
      />
    </div>
  );
}
