# Dispatch economics

ADR 0057 adds `PQPlanningInput`, `PQPlanningWitness`, `pq_planning_rows` and
`pq_witness_violation` for the formal single-microgrid nominal P/Q transition.
The module reuses only public control value contracts, never control services.
Actual P/Q and per-minute future ceilings bind independent resource commands;
PCS interval hold preserves economic energy integration. These witnesses do
not replace robust execution/AC/tracking certificates. Unsupported backends
reject joint inputs explicitly. See
[joint P/Q planning](../../../../docs/modeling/Single_PQ_Planning.md).

ADR 0056 adds `ACRecoveryPolicy`, `ACRecoveryEvidence` and the public
`run_ac_feasibility_recovery` application. Injected numerical/certification and
clock ports bound restoration and retain certified incumbents. Original
optimality is separate from the zero feasibility objective. The numerical
bridge uses one Branch Flow builder for cone and equality modes. See
[AC physical recovery](../../../../docs/modeling/AC_Physical_Recovery.md).

ADR 0053 adds `ReserveRecoveryTask`, `ReserveDeliveryPolicy` and `observe_reserve_recovery`. Reserve planning accepts an explicit remaining absolute recovery time. Recovery state is advanced only from observed feedback; planning and numerical retries cannot reset it. See [reserve recovery and delivery](../../../../docs/modeling/Reserve_Recovery_Delivery.md).

ADR 0050 adds `StorageWindowTarget` and `api.storage_window_target`: necessary energy reachability bounds from an absolute task, with a separate soft reference. These bounds are not a joint feasibility certificate. The single workflow reprices admitted feedback candidates through `api.evaluate_economics`; whole-study contractual economics retain their original comparison basis.

ADR 0039 adds `CoordinationRecoveryPolicy` and
`api.equivalent_coordination_calibration`: bounded retry/stagnation parameters
and a pure comparison of effective P/Q corrections. It does not certify a
feasible region. The cross-layer loop belongs to the cluster execution workflow;
SDK calls stay in composition adapters. Calibration v1 now supports explicit
same-version JSON round trips while rejecting unknown versions. See
[Coordination_Feasibility_Recovery.md](../../../../docs/modeling/Coordination_Feasibility_Recovery.md).

ADR 0035 corrects the reactive planning model to `reactive-planning-v2`:
`api.plan_reactive_envelope` now accepts `execution_step_minutes` (minutes,
default 1), checks whole-substep grids and initial capability at the current P,
and bounds the first substep so the constructed constant command never activates
the shared ramp limiter. This is a conservative reachable inner envelope at
fixed P, not a guarantee that feedback arbitration follows that witness or that
the entire AC transition is safe. No initial observation means no certificate
for the first transition; partial initial coverage is rejected.
New `ReactivePlanningPolicy()` values default to v2. Explicit v1 remains readable
and replays the historical endpoint-only rows without acquiring v2 guarantees;
there is no announced removal date. Returned rows are immutable, solver-free,
and consumed by all three existing backends. Inputs are unchanged; invalid or
unsupported grids fail explicitly. See the proof, conservatism and V2 disclosure
amendment in [Reactive_Tracking.md](../../../../docs/modeling/Reactive_Tracking.md).

ADR 0034 keeps planning model `storage-reserve-v1` and adds the recorded
`StorageReservePolicy.observation_tolerance_mw` for the workflow's v2 minute
observation. The observation model and duration are recorded separately;
planning envelopes are not a continuous reserve-deliverability certificate.

ADR 0031 adds `api.solve_for_quality` and typed quality/budget evidence. Only
budget exhaustion retries; infeasible/unknown/cancelled paths do not acquire
success. The numerical AC bridge supplies observations; the pure API owns the
bounded decision. See [Computation_Quality.md](../../../../docs/modeling/Computation_Quality.md).
Tests: `tests/unit/dispatch/test_computation_quality.py`,
`tests/integration/dispatch/test_computation_quality.py`.

ADR 0030 adds `api.plan_reactive_envelope`: immutable physical-facet reserve and
finite-response rows shared by the convex, MISOCP and linear bridges. Initial Q
comes only from completed device checkpoints. See
[Reactive_Tracking.md](../../../../docs/modeling/Reactive_Tracking.md).

ADR 0029 adds `api.plan_storage_reserve`, immutable `StorageReservePlan` and
versioned `StorageReservePolicy`. Forecast-derived power/energy margins are
planning envelopes, separate from physical storage ratings. The numerical
adapters share these constraints and refresh them when reusing a model.
See [Storage_Reserve.md](../../../../docs/modeling/Storage_Reserve.md).

ADR 0024 adds job-owned DPP model reuse to the legacy numerical adapters and
process-isolated regional realization at the bootstrap boundary. Public accounting
and contracts remain solver independent. See `tests/integration/dispatch/test_coordination_workspace.py`.

ADR 0023 corrects the existing `regional_control.py` numerical adapter to use
per-resource planned P/Q and the execution layer's declared capability limits.
No SDK or new backend is imported into this public accounting module. Day-ahead,
rolling and autonomous coordination share the same corrected resource model.
See [model and limits](../../../../docs/modeling/Coordination_Resource_Capabilities.md)
and `tests/integration/dispatch/test_coordination_capabilities.py`.

Service schema 1.5.0 may attach a separately computed day-ahead regional
realization cost. `realization_status=computed` only confirms that cost evidence
exists; it does not certify tracking, execution safety or the 10% requirement.
Missing/failed realization keeps its cost null. See ADR 0018.

Owns `dispatch-economics-v2`: renewable curtailment accounting, weighted operating
cost and comparable aggregate reference costs. It does not solve a network, run
SCIP/CVXPY, read files, issue commands or certify the customer's 10% requirement.

Public operations: `api.account_renewable`, `api.evaluate_economics`,
`api.assess_reference_economics`, `api.capture_coordination_snapshot`.
Immutable values live in `contracts`.
`CoordinationNetworkCalibration` and `FixedShuntReference` separately preserve
series losses and signed voltage-dependent fixed-shunt injections. Public APIs
`build_coordination_calibration`, `coordination_shunt_injection` and
`slice_coordination_calibration` copy/validate reference evidence and select rolling
intervals without importing a grid model or solver. Missing compensation evidence
is rejected when the current resolved network contains nonzero compensation;
old loss-only calls remain supported for networks with no effective fixed shunt.
See [calibration model](../../../../docs/modeling/Coordination_Network_Calibration.md)
and [ADR 0037](../../../../docs/architecture/decisions/0037-coordination-fixed-shunts.md).
Tests: `tests/unit/dispatch/test_network_calibration.py` and
`tests/integration/dispatch/test_fixed_shunt_coordination.py`.
`contracts.PCCTrackingLimits` additionally owns per-interval PCC P/Q bounds and
the separate numerical comparison allowance (ADR 0021). Legacy linear/MISOCP
adapters impose the engineering bounds; the workflow injects its recorded policy.
See [PCC tracking model](../../../../docs/modeling/PCC_Tracking_Limits.md).
Inputs are chronological interval MW and CNY/MWh with an explicit constant hour
duration. The caller resolves asset IDs and applies rated limits per resource
before aggregating. All public operations validate finite values and dimensions,
raise `ValueError` for invalid input, and never mutate input arrays.

Economics contracts/application/domain use only the standard library; adopted
schedule archive validation also uses the project's approved Pydantic. There are no I/O
ports to instantiate. Legacy solvers consume the public API; this module never
imports the legacy model, service, reporting or solver SDKs. New numerical
backends will be migrated separately, per GOV-04; this increment does not claim
the entire legacy optimizer complies with the target architecture.

Owners: 优化调度维护负责人. Review responsibility: 仿真研究维护负责人.
Model: [Dispatch economics v2](../../../../docs/modeling/Dispatch_Economics.md).
Decision: [ADR 0004](../../../../docs/architecture/decisions/0004-dispatch-economics.md).
Tests: `tests/unit/dispatch`, `tests/integration/dispatch`,
`tests/architecture/test_dispatch_boundaries.py`.

Compatibility: original raw availability output is retained; new fields separate
dispatchable availability, nameplate excess and dispatch curtailment. Cost units
remain CNY. New result calculation is marked `dispatch-economics-v2`; historical
unversioned accounting must not be silently relabelled or recomputed without
its original input and dispatch. Service v1.1 retains the numeric comparison
aliases while exposing typed, scoped reference accounting. Prefer that typed
field; aliases are retained for at least the project's 90-day/two-release window
after any future formal deprecation notice. No removal date is imposed here.

ADR 0005 adds `DispatchCapabilities` and `coordination-snapshot-v1` without
changing economics v2. Snapshot capture copies complete same-epoch regional
plans, references, previous references and scaled duals into tuples; it computes
their residuals and binds resource/cost outputs to that update. It rejects
partial, mixed-epoch or nonfinite evidence and inconsistent disabled storage.
The ADMM orchestrator alone applies the consecutive-update convergence rule.
A convex snapshot does not certify AC realization or device execution.
Service 1.2 publishes this evidence and explicitly scoped cluster validation.

ADR 0014 adds immutable `CoordinationIterationTrace` / `RegionalCoordinationTrace`
for service 1.4 replay. They preserve the coordinator's latest accepted P/Q
proposal, response epoch/send tick, post-update reference, effective outage,
local fallback and barrier eligibility, with a separate chronological hour axis.
Missing proposals are null, never initial autonomous estimates. This diagnostic
evidence does not change convergence or certify execution. Historical history
without traces remains readable; new strictly validated traces reject nonfinite,
misaligned, duplicate-region and future/mixed-epoch data. Arrays are copied to
tuples, accepting both JSON and parsed JSON at the boundary.
See [ADR 0014](../../../../docs/architecture/decisions/0014-coordination-replay.md).
Additional tests: `tests/unit/dispatch/test_coordination_trace.py`,
`tests/integration/dispatch/test_coordination_replay.py`.

ADR 0011 adds `adopted-schedule-v1`: explicit timestamps, immutable per-resource
commands and per-interval source windows, with no borrowed optimizer certificate.
`api.adopt_first_steps`, `api.slice_adopted_schedule` and `api.read_adopted_schedule`
own assembly, slicing and archive validation. Whole-adopted-horizon cost is not
computed and global optimality is not assessed. Window certificates remain in
their own result files. The reader preserves historical unversioned archives as
`LegacyAdoptedScheduleRecord`, whose original objective/cluster scope is unverified;
this historical type is not an executable schedule. New writes use the new schema.
# 集群功率因数参考收紧

公开 `cluster_power_factor_tracking_margin` 与 `tighten_cluster_power_factor_reference`，
以跟踪误差界限制反馈收紧幅度；不执行潮流校核或决定跨层采用。
见 [模型与证明边界](../../../../docs/modeling/Cluster_Power_Factor.md)、ADR 0042。

## 网络候选反馈增量

公开 `api.select_network_candidates` 及 `contracts.NetworkCandidatePolicy`、
`RegionalNetworkCandidate`、`NetworkCandidateSelection`。输入为已由工作流认证的
同窗口完整 P/Q 候选；模块只做有限联合选择，不求解潮流、不调用 SDK、不认证执行。
维护负责人：优化调度维护负责人；复核：潮流计算维护负责人。
模型与测试见 [Network_Candidate_Feedback.md](../../../../docs/modeling/Network_Candidate_Feedback.md)、ADR 0045。
旧恢复策略缺字段保持未启用；新字段及枚举需要严格消费者升级。
# 单微电网统一算法补充

ADR 0046 的正式单微电网继续使用经济规划和无功动态内近似产生候选；安全采用由统一工作流独立认证。
`assess_single_economic_requirement` 按已确认的无储能优化方案与选定方案计划综合成本判定至少 10%，不以执行安全替代经济门槛。
额外规划功率因数余量用于候选质量，失败可按原输入规划目标重试，物理安全门槛保持一致。

## 储能实际有功可达性

公开 `storage_reachability_rows`、`assess_storage_reachability` 和
`StorageReachabilityInput/Policy/Evidence`。本模块负责纯仿射约束与独立名义证书，
不读取设备状态、不驱动执行。CVXPY、MILP、MISOCP 后端通过既有桥使用相同约束。
负责人：优化调度维护负责人；复核：设备控制维护负责人。
方程、保守性、启动假设及验证入口见
[Storage_Reachability.md](../../../../docs/modeling/Storage_Reachability.md)、ADR 0047。

## ??????????2026-10-07?

?????? `NetworkCoordinationPolicy`?`JointNetworkTarget`?`JointNetworkEvidence`?
???????? SOCP ????????????????????/PF????????
?????????????????????????????????????????
??? `docs/modeling/Network_Physical_Recovery.md`?ADR 0051??????
`tests/integration/dispatch/test_network_physical_recovery.py`?


ADR 0055：`cluster_import_tracking_margin` 按区域数及 P 工程/数值容差计算参考容量预算；两种集群参考求解方式共用它。该预算不改变物理容量或最终联合检查，且与备用调用余量分开记录。验证：`tests/unit/dispatch/test_cluster_capacity_margin.py`。
