# Cluster import assessment

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
