# Cluster import assessment

ADR 0046 uses the same all-input AC certificate in the sole formal single workflow.
`radial_point_response` and `radial_proposal_margins` accelerate proposal search only;
point/corner checks never authorize execution. Exact certificate acceptance, outward
grid enclosure and bounded immutable caching remain independent of numerical solvers.

ADR 0044 adds `certify_ac_box`: an all-inputs radial AC disc certificate with
exact rational acceptance inequalities, fixed shunts, taps and both branch ends.
It proves the voltage/thermal/PCC/PF envelope instead of assuming it. Its
quasi-static scope and uniqueness conditions are in
[Conditional_Closed_Loop.md](../../../../docs/modeling/Conditional_Closed_Loop.md).

ADR 0040 adds `thermal_loss_upper_bound`: a pure, outward-rounded receiving-end
I²R envelope. Its continuous voltage/thermal assumptions are explicit; it does
not prove an AC trajectory. Inputs retain the resolved base, resistance, MVA
ratings and voltage lower bound. See
[Cluster_Transition.md](../../../../docs/modeling/Cluster_Transition.md).

Owners: 潮流计算维护负责人. Reviewer: 设备控制维护负责人.

Public APIs: `assess_cluster_import`, `summarize_cluster_validation`; immutable
contracts: `RegionalImport`, `ClusterImportAssessment`, `ClusterValidation`.
This increment evaluates existing synchronous PCC import trajectories. It does
not solve AC flow, optimize, resample, dispatch resources or hide violations.
Legacy AC iteration remains in its existing adapter. The A08 increment adds
`assess_power_balance`, `constant_power_current`, and `validate_bus_demands` via
the public API, with `FlowNumerics`, `RadialBalanceBranch`,
`PowerBalanceResidual`, and `PointInputValidation` contracts.

Positive power is import in MW. Time is elapsed minutes with a shared simulation
origin; callers must supply explicit region IDs, identical strictly increasing
time arrays, finite power and a validity flag for every point. Invalid settings
raise ValueError; incomplete/invalid evidence is unknown with nullable metrics.
Not-computed is explicit. Default 1e-5 MW is a software comparison tolerance,
not an approved field threshold. No data or input arrays are modified.

No other modules, I/O, framework or solver dependencies. Numerical validation
uses approved NumPy array operations; contracts remain stdlib-only. This is a small pure
use case, without unnecessary adapter/port layers. Contracts contain only
stdlib immutable values. `cluster-validation-v1` distinguishes the assessed
reference scope from execution status. Missing historic evidence stays missing.

Tests: `tests/unit/power_flow`, `tests/integration/dispatch/test_cluster_repairs.py`,
`tests/architecture`. See ADR 0005 and `docs/modeling/Cluster_Validation.md`.

AC/input validity tests: `tests/unit/power_flow/test_numerics.py`,
`tests/integration/power_flow/test_network_repairs.py`. See ADR 0009 and
`docs/modeling/Flow_Convergence.md` for units, equations, tolerances and failure semantics.
# 最终计划集群功率因数

公开 `assess_cluster_power_factor` 按同步时段汇总带符号 PCC P/Q，
缺失、未定义零流等保持 unknown，独立于调度参考进行评价。
模型与数值语义见 [Cluster_Power_Factor](../../../../docs/modeling/Cluster_Power_Factor.md)。

ADR 0049 补全 AC 圆盘证书的支路电流幅值外包。`certified_ac_loss_bounds` 用电流平方与固定并联项构造 P/Q 网损边界。
`refine_ac_balance_certificate` 仅在调用方已证明总节点需求约束时收紧 PCC，保留电压、支路和收敛检查；不能修复其失败。
适用范围和额外总需求约束随证书记录。二节点解析电流/损耗以及带变比、固定并联项的独立节点验证在
`tests/unit/power_flow/test_ac_envelope.py`。
