# Dispatch economics

Owns `dispatch-economics-v2`: renewable curtailment accounting, weighted operating
cost and comparable aggregate reference costs. It does not solve a network, run
SCIP/CVXPY, read files, issue commands or certify the customer's 10% requirement.

Public operations: `api.account_renewable`, `api.evaluate_economics`,
`api.assess_reference_economics`, `api.capture_coordination_snapshot`.
Immutable values live in `contracts`.
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
