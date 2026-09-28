# 区域 PCC 逐时段跟踪限制

模型标识：`pcc-tracking-limits-v1`。用于离线区域计划落实；P/Q 正值表示 PCC 受电。
负责人：优化调度维护负责人。决策见 [ADR 0021](../architecture/decisions/0021-pcc-tracking-limits.md)。

对每个区域、每个优化时段 t，输入目标 P_ref(t)（MW）、Q_ref(t)（Mvar）以及
正的工程限值 εP、εQ。模型引入非负变量 dP(t)、dQ(t)，施加：

```text
 P_PCC(t) - P_ref(t) ≤ dP(t)
 P_ref(t) - P_PCC(t) ≤ dP(t) ≤ εP
 Q_PCC(t) - Q_ref(t) ≤ dQ(t)
 Q_ref(t) - Q_PCC(t) ≤ dQ(t) ≤ εQ
```

等价于每时段 `|P_PCC-P_ref|≤εP`、`|Q_PCC-Q_ref|≤εQ`。
原目标函数中的 `Σ Δt × (wP dP + wQ dQ)` 和经济性项保留，在可行域内选择计划。
工程限值不因电价、惩罚权重、窗口长度或时段数量改变，不以平均误差判定。

集群默认 εP=0.05 MW、εQ=0.05 Mvar。它们是仿真假设，不是现场验收标准。
`ExecutionPolicy.tracking_limits` 将有效配置转换为调度模块不可变契约。
两个后端只负责各自 SDK 表达；P/Q 误差评价共用契约中有单位的比较方法。
没有提供限制的其他求解用例保留历史软惩罚语义。

数值校核独立计算 `max(abs(输出功率-输入目标))`，不使用求解器偏差辅助变量
代替实际误差。允许工程限值之外最多 `numerical_tolerance` 的浮点残差，默认
1e-6 MW/Mvar；模型本身上界仍为 εP/εQ。数值容差必须为正且小于工程限值。
例如 0.05000001 MW 可被识别为浮点边界误差，0.050002 MW 仍判超限。

这些约束叠加在原储能电量、设备功率/容量、风机无功权限、电压、PCC 功率因数、
防倒送等约束上，不替代或放松它们。因此过于严格或超出实际能力的目标可能无解。
成功输出仍需设备、独立 AC 和逐时段跟踪检查；区域规划点通过不保证设备每分钟
过渡响应通过，后者继续独立评价。线性后端仍是原线性近似，不能冒充 AC 证书。

新集群日内计划另施加 [储能计划备用与分钟反馈](Storage_Reserve.md) 约束，
为所配置的预测误差预留调节空间；仍不等同于所有扰动下的动态跟踪保证。

验证入口：`tests/unit/dispatch/test_pcc_tracking_limits.py`、
`tests/integration/dispatch/test_pcc_tracking_limits.py`、
`tests/integration/service/test_cluster_tracking.py`。
