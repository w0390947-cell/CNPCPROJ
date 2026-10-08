import type { SingleExecution } from '../../shared/api/generated/single-execution';

type Props = {
  execution: SingleExecution;
  economicPassed?: boolean;
  improvementPercent?: number;
};

export function SingleExecutionPanel({
  execution: e,
  economicPassed,
  improvementPercent,
}: Props) {
  const u = e.unified;
  const steps = u?.steps ?? [];
  const admissions = u?.target_admissions ?? [];
  const accepted = admissions.filter((a) => a.plan != null);
  const rejected = admissions.filter((a) => a.plan == null);
  const adjusted = accepted.filter((a) => {
    const t = a.plan?.tracking;
    return (
      a.origin === 'economic' &&
      t != null &&
      (t.target_p_mw !== a.requested_p_mw ||
        t.target_q_mvar !== a.requested_q_mvar)
    );
  }).length;
  const economic = steps.filter((s) => s.disposition === 'economic').length;
  const retained = steps.filter(
    (s) => s.disposition === 'retained_feedback',
  ).length;
  const renewals = admissions.filter((a) => a.origin === 'retained').length;
  const failedPlanning = (u?.economic_proposals ?? []).filter(
    (p) => p.command == null,
  ).length;
  const backup = steps.filter((s) => s.disposition === 'backup').length;
  const terminal = steps.filter((s) => s.disposition === 'terminal').length;
  const allocationTrials = accepted.flatMap(
    (a) => a.plan?.tracking?.allocation_trials ?? [],
  );
  const sharedFeedback = steps.filter((s) => {
    const p = u?.plans?.[s.plan_index]?.tracking?.policies?.[s.plan_offset];
    return (
      p != null &&
      [p.p_participation, p.q_participation].some(
        (weights) => (weights ?? []).filter((w) => w > 0).length > 1,
      )
    );
  }).length;
  const filtered = steps.filter(
    (s) =>
      s.disposition === 'economic' &&
      s.proposed_command != null &&
      (s.command.p_mw.some((p, i) => p !== s.proposed_command?.p_mw[i]) ||
        s.command.q_mvar.some((q, i) => q !== s.proposed_command?.q_mvar[i])),
  ).length;
  const percent = (value?: number) =>
    value == null ? '未记录' : `${(value * 100).toFixed(2)}%`;
  return (
    <section className="panel" aria-label="单微电网执行反馈">
      <h2>单微电网执行反馈</h2>
      <p>
        已完成 {e.completed_minutes} / {e.total_minutes} 分钟。{e.reason}
      </p>
      {u ? (
        <>
          <table>
            <thead>
              <tr>
                <th>评价项目</th>
                <th>本次结论</th>
                <th>适用范围</th>
              </tr>
            </thead>
            <tbody>
              <tr>
                <td>硬任务续接保证</td>
                <td>
                  {u.hard_task?.guarantee === 'maintained'
                    ? '持续保持'
                    : u.hard_task?.guarantee === 'not_required'
                      ? '本次无此任务'
                      : u.hard_task?.guarantee === 'assumption_violated'
                        ? '声明条件已失效'
                        : '硬任务未获保证'}
                </td>
                <td>
                  {u.hard_task?.outcome === 'met'
                    ? '原期限任务已完成'
                    : u.hard_task?.outcome === 'missed'
                      ? '原期限任务未完成'
                      : u.hard_task?.outcome === 'not_required'
                        ? '不适用'
                        : '尚未到期'}
                  {u.domain.energy_task
                    ? `；期限 ${u.domain.energy_task.deadline_step} 分钟，能量区间 [${u.domain.energy_task.energy_mwh.low}, ${u.domain.energy_task.energy_mwh.high}] MWh`
                    : ''}
                </td>
              </tr>
              <tr>
                <td>目标接纳认证</td>
                <td>
                  {u.target_admission_status === 'passed'
                    ? '通过'
                    : u.target_admission_status === 'rejected'
                      ? '存在未接纳目标'
                      : '尚无接纳证据'}
                </td>
                <td>
                  接纳 {accepted.length} / {admissions.length} 个，拒绝{' '}
                  {rejected.length} 个；接纳前调整内部目标 {adjusted} 个
                  ；其中原目标续接认证 {renewals} 个
                </td>
              </tr>
              <tr>
                <td>安全与备用续接</td>
                <td>
                  {u.status === 'passed'
                    ? '通过'
                    : u.status === 'assumption_violated'
                      ? '声明条件已失效'
                      : '未取得证书'}
                </td>
                <td>声明模型与扰动包络内的准静态安全</td>
              </tr>
              <tr>
                <td>PCC 期限后跟踪</td>
                <td>
                  {u.tracking_status === 'passed'
                    ? '通过'
                    : u.tracking_status === 'violated'
                      ? '未通过'
                      : '尚无到期证据'}
                </td>
                <td>
                  已执行周期末，有功 ±{u.domain.p_band_mw} MW、无功 ±
                  {u.domain.q_band_mvar} Mvar
                </td>
              </tr>
              <tr>
                <td>计划经济性改善至少 10%</td>
                <td>
                  {economicPassed == null
                    ? '未评价'
                    : economicPassed
                      ? '通过'
                      : '未通过'}
                  {improvementPercent != null
                    ? `（${improvementPercent.toFixed(2)}%）`
                    : ''}
                </td>
                <td>无储能优化方案与选定方案的计划综合成本</td>
              </tr>
            </tbody>
          </table>
          <p>
            经济策略执行 {economic} 次（其中安全过滤 {filtered} 次），备用续接{' '}
            {backup} 次，原目标反馈续接 {retained} 次，安全终态保持 {terminal}{' '}
            次。
          </p>
          <p>边界来源：{u.domain.bound_source}</p>
          <p>
            原目标优先；同目标获证候选先比较指令干预量，再比较预测成本。
            已接纳目标中记录分配试验 {allocationTrials.length}{' '}
            次，多资源反馈执行 {sharedFeedback} 次。
            有限构造未获证不等于物理不可达。
          </p>
          <details>
            <summary>查看保证前提与反馈证据</summary>
            <p>
              固定拓扑及控制权限，实际负荷和风光可用量留在声明包络内，设备响应符合所声明模型。目标接纳前联合认证反馈策略、期限可达性和安全续接；未接纳时继续原有尾部。
            </p>
            <p>
              跟踪证书覆盖到本次记录的接纳终点；取得硬任务联合证书时，续接同时保障原期限任务。更新失败仍按原目标与期限评价，不删除到期周期；外部固定目标不被改写。
              经济规划不可用时，可重新认证原目标的有限反馈窗口；认证失败则执行原安全尾部。
            </p>
            <p>
              安全保证不自动代表经济目标跟踪通过。周期末跟踪结果不构成周期内连续跟踪或现场暂态稳定证明。
            </p>
            <p>
              负荷相对误差 {percent(u.domain.research.load_relative_error)}
              ，风电与光伏预测扣减分别为装机的{' '}
              {percent(u.domain.research.wind_nameplate_error)} 和{' '}
              {percent(u.domain.research.pv_nameplate_error)}
              。显式固定包络输入以记录的逐节点边界为准。
            </p>
            <p>
              末段预测包络持续有效；设备周期末收敛分辨率{' '}
              {u.domain.research.settling_resolution} MW/Mvar；备用搜索上限{' '}
              {u.domain.research.backup_maximum_steps} 个周期。
            </p>
            <p>
              已记录 {u.economic_proposals?.length ?? 0}{' '}
              次从实际状态发起的经济规划，其中 {failedPlanning}{' '}
              次未取得可执行方案；
              {u.plans?.length ?? 0} 份采用证书。原目标续接不计作经济规划成功。
            </p>
            {admissions.length > 0 && (
              <details>
                <summary>查看目标接纳与保证时段</summary>
                <table>
                  <thead>
                    <tr>
                      <th>起止分钟</th>
                      <th>来源</th>
                      <th>请求 P / Q</th>
                      <th>接纳 P / Q</th>
                      <th>期限</th>
                      <th>分配认证</th>
                    </tr>
                  </thead>
                  <tbody>
                    {admissions.map((a) => {
                      const t = a.plan?.tracking;
                      return (
                        <tr key={a.start_step}>
                          <td>
                            {a.start_step} — {a.end_step}
                          </td>
                          <td>
                            {a.origin === 'external'
                              ? '外部固定'
                              : a.origin === 'retained'
                                ? '原目标续接'
                                : '内部规划'}
                          </td>
                          <td>
                            {a.requested_p_mw.toFixed(4)} /{' '}
                            {a.requested_q_mvar.toFixed(4)}
                          </td>
                          <td>
                            {t
                              ? `${t.target_p_mw.toFixed(4)} / ${t.target_q_mvar.toFixed(4)}`
                              : `未接纳：${a.reason}`}
                          </td>
                          <td>{t?.deadline_step ?? '未承诺'}</td>
                          <td>
                            {t
                              ? `候选 ${t.construction}；${t.allocation_trials?.filter((x) => x.status === 'certified').length ?? 0} 个获证`
                              : '构造未获证'}
                          </td>
                        </tr>
                      );
                    })}
                  </tbody>
                </table>
                <p>
                  功率单位 MW /
                  Mvar；保证截止至记录的终点分钟，原始请求与接纳目标分别保存。
                </p>
              </details>
            )}
          </details>
        </>
      ) : (
        <p>参考规划未通过执行前检查，尚未启动统一算法执行。</p>
      )}
      <p className="text-sm text-muted-foreground">
        执行从声明初态开始，启动前过渡未评估；后续规划继承已完成的 P/Q
        和储能能量。经济指标及参考曲线采用计划口径。
      </p>
    </section>
  );
}
