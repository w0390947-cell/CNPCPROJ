# 集群自动执行校核

ADR 0056 为区域与联合参考增加 `ac-feasibility-recovery-v1`。全部物理检查通过且显式授权时，恢复候选可带经济最优性 `unknown` 继续采用审核；其他未知项仍阻止采用。现有联合校核和 v6 安全监督保留，旧记录不补证。见 [AC 恢复模型](../../../../docs/modeling/AC_Physical_Recovery.md)。

## 当前正式执行：v6

ADR 0058 删除旧设备会话、过渡提交及专属备用装配桥。当前滚动引擎要求安全策略，初始认证失败时拒绝推进；历史结果的数据契约保留，不再提供旧执行入口。清退及验证范围见 [ADR 0058](../../../../docs/architecture/decisions/0058-retire-legacy-device-executor.md)。

ADR 0055：`ClusterSafetyController` 统一管理普通经济动作、认证后备、终端控制及已接纳备用事件。所有动作同时满足完整连续区间设备与联合 AC 约束，并保留认证后续路径。新经济优化失败时可执行已有后备；原期限和备用承诺不取消。初始域未获证明或实际输入越域时停止并保留证据。

`api` 公开监督器、完整备用策略、参考恢复和 `replay_cluster_safety`；`contracts` 提供显式域、状态、路径和结果。纯算法仅依赖 control、dispatch、power_flow 的公开 API/契约与 kernel，不读取未来实绩或调用数值 SDK。bootstrap 负责既有算例投影及经济求解适配。

正式网页、CLI 和批量研究统一装配 `cluster-rolling-v6`，停用储能也保留网络/设备/后续可行性门禁，但不承诺储能备用。完整证据存于 `ClusterExecution.safety`；逐分钟实际选择和原经济目标分开记录。`RollingUpdate.continuation_available` 使失败的经济窗口可以继续执行认证后备。

维护：仿真研究、设备控制、优化调度维护负责人；物理证明复核：潮流计算维护负责人。
模型及归纳论证见 [Cluster_Recursive_Safety.md](../../../../docs/modeling/Cluster_Recursive_Safety.md)，验证见同目录验证文档。

核心测试：`tests/workflows/test_cluster_safety.py`、`test_cluster_interval_constraints.py`、`tests/unit/control/test_cluster_tube.py`；正式入口及故障恢复见 `tests/integration/service/test_cluster_execution.py`、`test_reserve_delivery.py`、`test_reserve_feedback.py`。安装包回放工具为 `tools/verify_cluster_safety.py`。

## 以下为 v1—v5 的历史演进记录

下列“新默认”“停止”等表述只描述对应版本；当前采用规则以上面的 v6 说明为准。旧记录不补发新版证书。

ADR 0053：`certify_reserve_service`、`certify_cluster_reserve` 与 `reserve_activation_command` 提供连续区间的充分认证及可执行因果调用策略。`ReserveRecoveryTask` 在统一滚动路径中跨窗口保留，实际反馈才能销账；默认正式备用路径为 `cluster-rolling-v5`。证书未知时不伪造通过，完整支撑时间不在研究末端缩短。见 [模型及边界](../../../../docs/modeling/Reserve_Recovery_Delivery.md)。

ADR 0040：`prepare_transition_round` / `transition_commits` 通过公开 control API
形成联合区间许可，并在所有区域重新完成独立端点校核后跨过采用屏障。
新滚动执行强制启用 `cluster-transition-box-v1`；条件证明、未通过及已提交
分别记录。历史字段保持 None/空，不补造证据。结果模式 1.10.0，新增
`cluster_transitions`。详细假设、保守性与失败语义见
[Cluster_Transition.md](../../../../docs/modeling/Cluster_Transition.md)。

ADR 0039：`api.run_reference_recovery` 编排同一窗口内的协调、严格落实、区域
诊断反馈与重协调。策略与校准重复判断消费 dispatch 公开 API/契约；泛型数值
载荷不在工作流中解析。诊断不能携带 P/Q 采用证书，最终采用仍需严格区域
检查和联合容量检查。历史策略/恢复记录缺省 None，主服务新默认启用最多
3 次重协调；结果模式 1.9.0，新增 `day_ahead_recovery` 与窗口
`reference_recovery`。日前目标可能经恢复修正，以最终区域记录为准，原
服务协调快照仍表示初始参考。模型、预算及专利边界见
[Coordination_Feasibility_Recovery.md](../../../../docs/modeling/Coordination_Feasibility_Recovery.md)。

ADR 0034：分钟观察改为 `storage-reserve-observation-v2`，先求可持续绝对目标
区间，再相对已完成实际功率计算增量。无效物理状态显式失败。观察记录支持小时数；
比较容差随策略保存。结果模式1.8.0；历史缺省观察版本为 None，不重算或重标记。
不改变规划备用模型、设备物理约束及未来命令采用规则。见
[Storage_Reserve.md](../../../../docs/modeling/Storage_Reserve.md)。
回归：`tests/workflows/test_minute_reserve.py` 与
`tests/integration/service/test_reserve_feedback.py`。

ADR 0031：`policy.computation_quality` 记录后台分级策略，日前区域和滚动窗口
保留优化质量、ADMM 累计预算及残差证据。区域最优性精度不足为 unknown，
不采用该计划；未收敛保留停止原因。历史缺省无质量证据。

ADR 0030：新默认日内计划通过 `reactive_planning` 保留无功余量与过渡约束，
窗口记录 `initial_reactive_mvar` 和独立 `REACTIVE_PLAN_ENVELOPE` 检查；
`reactive_tracking_version` 区分新执行器与历史结果。校核期限和工程限值不变。
见 [Reactive_Tracking.md](../../../../docs/modeling/Reactive_Tracking.md)。

ADR 0029：新默认运行记录 `policy.storage_reserve`，日内计划预留双向功率/电量。
`api.observe_storage_reserve` 每分钟用已完成状态观察余量；不足时由装配器联合重算
短窗口，原/新目标及采用校核保存于 `reserve_target_adjustments`，观察保存于
`minute_reserves`。失败保留已完成前缀；常规滚动计数不变。历史策略缺省 None、
证据缺省空；停用储能不产生备用证据。见
[Storage_Reserve.md](../../../../docs/modeling/Storage_Reserve.md)。

ADR 0028：新运行在 `policy.active_tracking_version` 记录分钟有功剩余分配模型，
历史记录缺失该字段时为 None；不改变旧结果的证据含义。普通反馈保留工程偏差限值。

ADR 0025：`run_feedback_loop` 可选 `rolling_progress` 输出窗口观察，
只有计划采用、分钟执行和电量反馈全部完成才增加计数。编号从 1 开始，
分钟是相对仿真起点的偏移；中止保留已完成前缀。观察不参与数值计算。
原文字回调保持兼容；无观察回调时计算与结果语义不变。

ADR 0024：新的默认数值装配启用协调模型复用、最多三个独立区域求解进程；
工作流时间顺序和采用条件保持不变。策略记录 `regional_plan_workers`、
`numerical_threads_per_worker` 和 `coordination_model_reuse`。历史缺省分别为
1、None、False，不补造并行或复用证据。资源和进程生命周期由 runtime/装配边界负责。

负责人：仿真研究维护负责人；复核：优化调度与设备控制维护负责人。
公开入口 `api.verify_execution`，契约 `contracts`。依赖 power_flow 的公开 API/契约
及 dispatch 的公开跟踪限值契约；
动态结果另依赖 control 公开契约，规则由其公开 API 在数值装配边界评价。
数值求解与设备仿真由装配边界注入，不依赖旧求解器模块或读取文件。

一次仿真自动依次校核日前区域落实、日内更新和分钟级响应。ADMM 未收敛时不执行目标。
各区域设备约束、独立网络复核、逐点 P/Q 跟踪和同步集群受电均纳入结论。
求解不可用或缺失证据保持 unknown/not_computed，越限保留 violated 及原始轨迹。
历史结果缺少此契约时不补造执行通过状态。

P/Q 正值为 PCC 受电；储能正值为放电。分钟目标是当时采用的日内计划；日前目标为
最终通过严格落实的 ADMM 参考，日内目标为每轮重新协调的 ADMM 参考。默认逐点跟踪容差
0.05 MW/Mvar 是软件仿真假设，随结果记录，非甲方验收定值。
输入均为离线模拟，不提供现场执行认证。见 ADR 0018。

测试：`tests/workflows/test_cluster_execution.py`、`tests/integration/service/test_cluster_execution.py`。

日内与分钟阶段现按 ADR 0020 交替推进：默认 15 分钟更新、4 小时预测窗口，
每轮用已执行电量初始化储能并重新进行集群协调。`api.run_feedback_loop` 负责
时间顺序，数值计算及连续设备会话由注入适配器负责。更新失败停止后续采用，
已执行证据不丢失。`rolling_updates` 保存各轮状态和目标，旧记录缺省为空。
日内曲线只拼接每轮首段，分钟误差对照当时采用的目标；窗口成本不重复累加。
相关回归：`tests/workflows/test_cluster_rolling.py`、
`tests/integration/control/test_device_session.py`。原逐点跟踪容差保持不变。

ADR 0021 将日前及滚动区域计划的逐时段 P/Q 限制加入优化模型，采用前统一调用
`api.executable_regional_plan` 检查设备、AC、P/Q 证据。工程限值与独立数值余量
共用已归档策略；证明确实不可行时记录 violated，无可用解的超时保持 unknown。
没有合格日前电量轨迹或滚动计划时自动停止后续执行，保留已有证据。

ADR 0022（结果 1.7.0）将分钟跟踪分为响应期限与期限后偏差，原始全程峰值
保留于 dynamic_tracking；工程限值、安全检查和计划硬约束保持不变。
历史策略缺省为 None、历史动态证据为空，不生成新通过结论。新计算自动启用。

ADR 0036 增加 `policy.device_capability_version`：当前装配记录
`device-capability-v2`，表示候选采用前按候选 P、原 Q 完成设备能力检查。
历史缺失时保持 None；有功分配版本独立保留。该字段不替代逐次校核结果。
契约验证入口：`tests/workflows/test_device_capability_policy.py`。

ADR 0038 增加 `policy.reactive_dynamics_version`，当前装配记录
`reactive-dynamics-v1`，标识真实周期初 Q 与单次普通响应的证据语义。
历史缺失为 None，逐周期记录缺失时保持未评估；不补造动态通过结论。
# 最终集群计划 PF 联合闭环

在既有参考恢复循环内增加逐时段联合 PF 校核与有界收紧参考分支。
日前、滚动、备用触发短窗口均须通过，失败候选不进入设备会话。
与区域校准共用恢复预算，保留每轮证据；历史记录不补造证明。
见 [模型说明](../../../../docs/modeling/Cluster_Power_Factor.md)、ADR 0042。

## 网络候选反馈增量

`candidates.py` 管理单次恢复窗口的认证候选、来源及一次性待尝试组合；
`recovery.py` 编排候选重协调、严格落实与原联合采用。选择规则只调用 dispatch
公开 API，具体求解器仍由 bootstrap 端口注入，不增加工作流对遗留模型的依赖。
默认服务启用 `network-candidates-v1`；显式旧策略仍无候选恢复。证据记录每次选择
和下一轮实际使用的候选，并验证其诊断来源。维护/复核职责沿用模块登记。
详见 [Network_Candidate_Feedback.md](../../../../docs/modeling/Network_Candidate_Feedback.md)、ADR 0045 及候选工作流/集成测试。

## 储能初态与名义可达证据

默认集群装配启用 `storage-reachability-v1`，算法身份提升为 `cluster-rolling-v3`。
区域严格落实和诊断均检查当前实际有功可达性与电量一致性；证书绑定同一设备、
时间轴和采用轨迹。滚动记录保存实际初始有功，常规与分钟重规划共用此边界。
证书缺失的历史结果不补证；名义通过不替代真实执行、保护或网络安全校核。
见 [Storage_Reachability.md](../../../../docs/modeling/Storage_Reachability.md)、ADR 0047。

## ????????????2026-10-07?

?? `cluster-rolling-v4`????? `coordination-recovery-v3`????????????
????????????????????????????????????????
????? `reference_method=joint-network` ? `joint_network`????? ADMM ???
?????????????????/PF ????????????????????
????????????????????????????
`docs/modeling/Network_Physical_Recovery.md`?
