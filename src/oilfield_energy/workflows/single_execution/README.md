# 单微电网统一执行

负责人：仿真研究维护负责人；复核：优化调度、设备控制与潮流计算维护负责人。依据 ADR 0046、0048、0049、0050、0052、0054、0057。
公开入口 `api.run_unified_feedback`、`api.UnifiedSingleController`、`api.verify_unified_execution`；证据 `contracts.SingleExecution` / `UnifiedExecution`。

`forecast.py` 从预测和研究假设构造域；`network.py` 统一设备时变能力与 AC 外包；`certificates.py`、`tasks.py` 组合安全和硬任务续接；`tracking.py` 认证有限反馈策略；`admission.py`、`economics.py` 选择并重新计价获证候选。
`unified.py` 独占证书采用、绝对时钟与一次状态提交；`run_unified.py` 调用注入的经济及当前观察端口。
经济数值求解在 bootstrap，执行使用已认证的同一反馈策略；所有正式输入共用一个执行器，不运行 ADMM。

每个命令连同有限备用及不变终态认证，失败候选保留旧尾部，储能每周期只积分一次。安全、期限后周期末跟踪、计划经济性分别报告。
条件与证明见 [Single_Microgrid_Execution.md](../../../../docs/modeling/Single_Microgrid_Execution.md)。
执行契约 `single-execution-v9` / `unified-single-v7` 只接受新版统一证据。显式边界由 `contracts.SingleBoundaryRequest` 提供；旧执行器和旧证据类型已移出正式包。历史结果使用归档环境，决策见 [ADR 0048](../../../../docs/architecture/decisions/0048-single-retirement.md)。

ADR 0057 增加经济名义 P/Q 过渡见证；`planning.validate_pq_proposal` 将其绑定到同一实际状态、设备、未来分钟边界、计划端点和能量。正式执行仍使用获证反馈，不直接下发名义过渡命令。见 [联合规划模型](../../../../docs/modeling/Single_PQ_Planning.md)。

`certificates.py` 调用 control 公开的 PCS 独立备用候选构造；每个参考比例保留替代候选，完整尾部或原任务失败时重试原缩放路径。候选选择不提交实际状态，所有路径仍经过同一套认证。模型及有限搜索边界见 [Single_PCS_Backup.md](../../../../docs/modeling/Single_PCS_Backup.md)。

目标接纳保存请求、调整、拒绝及有限保证时段。经济规划不可用时可重新认证原目标的有限反馈窗口，来源为 `retained`，执行为 `retained_feedback`。
`allocation.py` 构造时变分配和参与分数；固定原目标优先，同目标获证候选按干预量、预测成本排序。记录各目标认证结论和所选目标的分配试验；有限搜索未获证不能解释为物理不可达。公式及构造限制见 [Single_Local_Allocation.md](../../../../docs/modeling/Single_Local_Allocation.md)。
原目标与期限不变，失败经济规划仍保留；重新认证失败则继续原安全尾部，不隐藏后续跟踪失败。
跟踪修复和独立验证见 [ADR 0049](../../../../docs/architecture/decisions/0049-single-target-admission.md)。

硬任务保证与实际期限结果分别记录；初始联合证书未取得时继续安全仿真并明确未获保证。原始末能量区间与期限不随参考优化漂移；日内参考仅为软偏好。
模型、归纳前提及构造局限见 [Single_Task_Continuation.md](../../../../docs/modeling/Single_Task_Continuation.md)。工作流允许调用 dispatch 公开经济核算 API，不使用求解器 SDK。
