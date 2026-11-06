'use client';

import { useState } from 'react';
import type { SingleEconomicProposal } from '../../shared/api/generated/single-execution';
import { interval, value, type DisplayExecution } from './presentation';
import styles from './style.module.css';

export function PlanningEvidence({ u, proposal, onSelect }: {
  u: DisplayExecution; proposal?: SingleEconomicProposal; onSelect: (minute: number) => void;
}) {
  const [period, setPeriod] = useState(0);
  const [resource, setResource] = useState(0);
  const admission = u.target_admissions.find((a) => a.start_step === proposal?.start_step);
  const w = proposal?.pq_witness;
  const t = admission?.plan?.tracking;
  const index = Math.min(period, Math.max(0, (w?.commands.length ?? 1) - 1));
  const resources = u.domain.devices.resources;
  const ri = Math.min(resource, resources.length - 1);
  return <>
    <h3>滚动规划窗口</h3>
    <label>选择窗口 <select aria-label="选择滚动规划窗口" value={proposal?.start_step ?? ''} onChange={(e) => { setPeriod(0); onSelect(Math.min(Number(e.target.value) + 1, u.steps.length)); }}>
      {u.economic_proposals.map((p) => <option key={p.start_step} value={p.start_step}>第 {p.start_step} 分钟 · {p.result.status}</option>)}
    </select></label>
    {!proposal ? <p>未记录经济规划候选。</p> : <>
      <p>窗口起点为已完成第 {proposal.start_step} 周期的状态；本次规划状态：{proposal.result.status}。{proposal.result.reason}</p>
      <div className={styles.scroll}><table><caption>实际状态继承与经济候选 · P / MW，Q / Mvar</caption><thead><tr><th>资源</th><th>窗口初态 P / Q</th><th>过渡见证初态 P / Q</th><th>经济候选 P / Q</th></tr></thead><tbody>
        {resources.map((r, i) => <tr key={r.resource_id}><th scope="row">{r.resource_id}</th><td>{value(proposal.initial.p_mw[i])} / {value(proposal.initial.q_mvar[i])}</td><td>{value(w?.inputs.initial.p_mw[i])} / {value(w?.inputs.initial.q_mvar[i])}</td><td>{value(proposal.command?.p_mw[i])} / {value(proposal.command?.q_mvar[i])}</td></tr>)}
      </tbody></table></div>
      <p>实际初始电量 {value(proposal.initial.energy_mwh)} MWh；见证初始电量 {value(w?.inputs.initial.energy_mwh)} MWh。规划初态由后端与已完成状态核对。</p>
      {w ? <>
        <p>规划覆盖 {w.inputs.planning_steps * w.inputs.planning_minutes} 分钟，{w.inputs.planning_steps} 个计划区间，每区间 {w.inputs.planning_minutes} 分钟；前 {w.inputs.transition_minutes} 个一分钟子步构造联合过渡，其后保持计划端点。爬坡 / 整定构造利用系数 {w.inputs.ramp_utilization} / {w.inputs.settling_utilization}。</p>
        <div className={styles.toolbar}>
          <label>计划区间 <select aria-label="选择名义过渡区间" value={index} onChange={(e) => setPeriod(Number(e.target.value))}>{w.commands.map((_, i) => <option key={i} value={i}>{(w.inputs.start_step ?? 0) + i * w.inputs.planning_minutes}—{(w.inputs.start_step ?? 0) + (i + 1) * w.inputs.planning_minutes} 分钟</option>)}</select></label>
          <label>资源 <select aria-label="选择见证资源" value={ri} onChange={(e) => setResource(Number(e.target.value))}>{resources.map((r, i) => <option key={r.resource_id} value={i}>{r.resource_id}</option>)}</select></label>
        </div>
        <div className={styles.scroll}><table><caption>联合 P/Q 名义过渡见证与对应时刻执行记录</caption><thead><tr><th>完成分钟</th><th>名义命令 P / Q</th><th>命令起点 / 末态有功上限</th><th>实际命令 P / Q</th><th>实际末态 P / Q</th></tr></thead><tbody>
          {w.commands[index].map((c, j) => {
            const offset = index * w.inputs.planning_minutes + j;
            const k = (w.inputs.start_step ?? 0) + offset;
            const actual = u.steps[k];
            return <tr key={j}><th scope="row"><button onClick={() => actual && onSelect(k + 1)} disabled={!actual}>{k + 1}</button></th><td>{value(c.p_mw[ri])} / {value(c.q_mvar[ri])}</td><td>{value(w.inputs.p_ceiling_mw[offset]?.[ri])} / {value(w.inputs.p_ceiling_mw[offset + 1]?.[ri])}</td><td>{value(actual?.command.p_mw[ri])} / {value(actual?.command.q_mvar[ri])}</td><td>{value(actual?.after.p_mw[ri])} / {value(actual?.after.q_mvar[ri])}</td></tr>;
          })}
        </tbody></table></div>
        <p>区间计划端点 P / Q：{value(w.endpoints[index].p_mw[ri])} MW / {value(w.endpoints[index].q_mvar[ri])} Mvar；规划区间末电量 {value(proposal.result.storage_energy_mwh?.[index + 1])} MWh。名义过渡与实际反馈执行分别记录；未来区间的实际状态可能已受后续滚动规划影响，不能将两者视为同一轨迹。</p>
      </> : <p className={styles.notice}>本次候选未记录联合 P/Q 过渡见证。</p>}
      <details><summary>本窗口后台校核及求解质量</summary><div className={styles.scroll}><table><thead><tr><th>检查</th><th>后台状态</th><th>记录</th></tr></thead><tbody>{proposal.result.checks?.map((c, i) => <tr key={i}><th scope="row">{c.code}</th><td>{c.status}</td><td>{c.reason} · {String(c.actual ?? '未记录')} / {String(c.limit ?? '未记录')}</td></tr>)}</tbody></table></div>
        {(proposal.result.optimization_quality ?? []).map((q, i) => <p key={i}>求解 {i + 1}：{q.status}；{q.attempts.map((a) => `${a.budget_seconds}s / ${a.solver_status} / 相对间隙 ${value(a.relative_gap)}`).join('；')}</p>)}
        {proposal.result.ac_recovery && <p>AC 恢复：{proposal.result.ac_recovery.outcome}；原经济目标最优性：{proposal.result.ac_recovery.original_optimality}。</p>}
      </details>
    </>}
    <h3>目标请求与分配选择</h3>
    {!admission ? <p>该窗口未记录目标接纳。</p> : <>
      <p>原请求 P / Q {value(admission.requested_p_mw)} MW / {value(admission.requested_q_mvar)} Mvar；接纳 P / Q {value(t?.target_p_mw)} MW / {value(t?.target_q_mvar)} Mvar。原响应期限 D={t?.deadline_step ?? '未承诺'}，有限保证终点 G={t?.guaranteed_until_step ?? '未承诺'}。{admission.reason}</p>
      <div className={styles.scroll}><table><caption>同一接纳目标的分配构造 · 干预量优先，其次比较同口径预测费用</caption><thead><tr><th>构造</th><th>后台认证</th><th>指令干预量</th><th>预测费用 / 元</th><th>采用 / 原因</th></tr></thead><tbody>
        {(t?.allocation_trials ?? []).map((a) => <tr key={a.construction}><th scope="row">{a.construction}</th><td>{a.status === 'certified' ? '获证' : '未获证'}</td><td>{value(a.intervention, 9)}</td><td>{value(a.economic_cost_cny, 3)}</td><td>{a.construction === t?.construction ? '采用' : a.reason || '其他获证候选'}</td></tr>)}
      </tbody></table></div>
      <details><summary>原请求与替代目标的搜索记录</summary><div className={styles.scroll}><table><thead><tr><th>序号</th><th>尝试 P / Q</th><th>后台结果</th><th>预测费用 / 元</th><th>采用 / 原因</th></tr></thead><tbody>
        {admission.attempted_targets?.map((target, i) => <tr key={i}><th scope="row">{i}</th><td>{value(target[0])} / {value(target[1])}</td><td>{admission.candidate_outcomes?.[i]}</td><td>{value(admission.candidate_economic_costs_cny?.[i], 3)}</td><td>{admission.selected_candidate === i ? '采用' : admission.candidate_reasons?.[i] || '—'}</td></tr>)}
      </tbody></table></div></details>
      <p>费用评价口径：{admission.economic_scope}；仅对应获证候选的预测比较，不代表实际费用或全局最低费用。</p>
    </>}
    {t && <details><summary>首周期共享反馈分配</summary><div className={styles.scroll}><table><thead><tr><th>资源</th><th>P 参与权重</th><th>Q 参与权重</th><th>基准 P / Q</th></tr></thead><tbody>
      {resources.map((r, i) => { const p = t.policies[0]; return <tr key={r.resource_id}><th scope="row">{r.resource_id}</th><td>{value(p?.p_participation?.[i] ?? (p?.p_regulator === i ? 1 : 0))}</td><td>{value(p?.q_participation?.[i] ?? (p?.q_regulator === i ? 1 : 0))}</td><td>{value(p?.base.p_mw[i])} / {value(p?.base.q_mvar[i])}</td></tr>; })}
    </tbody></table></div><p>认证状态盒与指令策略按各自绝对周期位置保存；完整记录仍可导出。</p></details>}
    <p>原任务区间 {interval(u.domain.energy_task?.energy_mwh)} MWh；原截止周期 {u.domain.energy_task?.deadline_step ?? '无此任务'}，窗口软参考不重新定义原任务。</p>
  </>;
}
