# Dispatch economics

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
