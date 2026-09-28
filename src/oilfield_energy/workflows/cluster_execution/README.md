# 集群自动执行校核

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
原 ADMM 参考，日内目标为每轮重新协调的 ADMM 参考。默认逐点跟踪容差
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
