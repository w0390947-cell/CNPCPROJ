# Field initialization

Owner: 仿真研究维护负责人; reviewers: 优化调度维护负责人、设备控制维护负责人.

Public boundary: `api.InitializeRun.execute(contracts.InitializeRequest)` returns
`contracts.InitializeResponse`. The workflow validates normalized inputs, maps
explicit dated bounds, calls the existing single-grid safety/task certificate
APIs, and atomically stores a session. Default assembly remains shadow-only.
An explicitly injected synchronous software transport can admit `closed_loop`;
an opt-in bounded timing model is described below; D5000 commissioning remains
outside this workflow. See the execution
increment described below.

`api.UpdatePlanningInputs.execute(contracts.UpdatePlanningRequest)` returns
`contracts.UpdatePlanningResponse`. It stages validated revisions or activates
them only against a fresh matching confirmed boundary and new safety/task proofs.
The energy task is immutable in this increment. Staging never certifies a future
state; no background promotion or economic solve is performed. Initialization and
planning updates share this workflow's owned session storage and message namespace.
Original initialization evidence remains immutable; activation atomically commits
a revision and its current-context pointer. Historical replays preserve expiry.

`api.RequestPccTarget.execute(contracts.RequestPccTargetRequest)` returns
`contracts.RequestPccTargetResponse`. It certifies exactly the external P/Q,
absolute deadline and supported requested bands, including the original energy
task and safe continuation. It rejects missing current confirmed state and future
targets. The finite guarantee is explicit and may precede the requested hold.
Planning activation must re-certify the current target without resetting its
deadline or shortening its guarantee, atomically committing both context heads.
The shadow operations and execution increment share the owned message namespace
and persistence transaction.

`api.SubmitSnapshot.execute(contracts.SubmitSnapshotRequest)` returns
`contracts.SubmitSnapshotResponse`. It stores normalized observed facts and checks
quality, identity, ordering, explicit forecast coverage, device limits, fixed-state
AC/PCC consistency and applicable target evidence. It never confirms a cycle,
replaces completed state or promotes staged inputs. New contradictory/unconfirmed
observations can set a durable resynchronization barrier. Target admission and
planning activation honor that barrier; a later good snapshot does not clear it.
Latest ordered and latest consistent observations are separate from confirmed state.

Storage, content-addressed artifact reading, the clock and legacy network
projection are injected. A registered bootstrap bridge handles `NetworkModelV2`;
business code does not read raw files, import legacy network internals or depend
on FastAPI. SQLite preserves original requests, resolved model inputs, proof
data and responses. An existing microgrid session cannot be silently replaced.

Scope: exact normalized instantaneous state, one storage model, 1-minute clock,
explicit forecast endpoints and finite validity, supported fixed radial network.
PCC model-comparison tolerances are independent of target tracking tolerances.
Forecast interpolation is declared; no synthetic topology or research error
bounds replace missing field inputs. Estimated device/load state is not supported.
Wind/PV availability may be explicitly identified estimates.

Validation:

```text
python -m pytest tests/workflows/test_field_initialization.py tests/integration/service/test_field_initialization_http.py
python -m pytest tests/workflows/test_field_planning_updates.py tests/integration/service/test_field_planning_http.py
python -m pytest tests/workflows/test_field_pcc_targets.py tests/integration/service/test_field_targets_http.py
python -m pytest tests/workflows/test_field_snapshots.py tests/integration/service/test_field_snapshots_http.py
python tools/export_field_initialization_contracts.py --check
```

See [initialization guide](../../../../docs/guides/Field_Initialization.md) and
[ADR](../../../../docs/architecture/decisions/0054-field-initialization.md).

See also [planning update guide](../../../../docs/guides/Field_Planning_Updates.md)
and [planning update ADR](../../../../docs/architecture/decisions/0061-field-planning-updates.md).

See [PCC target guide](../../../../docs/guides/Field_Pcc_Targets.md) and
[target ADR](../../../../docs/architecture/decisions/0062-field-pcc-targets.md).

See [snapshot guide](../../../../docs/guides/Field_Snapshots.md) and
[snapshot ADR](../../../../docs/architecture/decisions/0063-field-snapshots.md).

In shadow mode, `api.PrepareControlCycle.execute(contracts.PrepareControlCycleRequest)` returns
`contracts.PrepareControlCycleResponse`. It uses the existing certified strategy
to prepare one shadow minute at the fresh initialized boundary. Commands and
predictions are separate immutable evidence; no confirmed state is advanced.
The shared single-execution preparation API is reused by the original simulation
controller. Target obligations and required energy tasks remain intact. New
observations, accepted targets and activated inputs atomically invalidate the
current shadow batch; historical replay never revives it.

See [control-cycle guide](../../../../docs/guides/Field_Control_Cycles.md) and
[ADR 0064](../../../../docs/architecture/decisions/0064-field-control-cycles.md).

```text
python -m pytest tests/workflows/test_field_control_cycles.py tests/integration/service/test_field_control_cycles_http.py
```

## Software execution increment

`api.DispatchControlCycle`, `api.ReportExecution`, `api.QueryRunStatus` and
`api.StopRun` own send claims, monotone device facts, observed-state confirmation,
query reconciliation and stopping. An explicit `api.ExecutionTransport` verifies
authority and implements send/query; bootstrap injects it. Default services have
no port and cannot dispatch. Only the `software_synchronous_v1` timing profile is
supported, with an injected boundary clock and zero lead; no live device timing
guarantee is implied.

Confirmed boundaries live separately from immutable initialization and predicted
endpoints. Preparation, later target requests and planning activation use the
current measured boundary and retain absolute obligations. A pending execution
cannot be overwritten by planning or an unrelated snapshot. Normal endpoint
observations need explicit ReportExecution confirmation before state advances.

Sending claims commit before external I/O; an uncertain crash never triggers
automatic resend. Query reconciliation runs outside the persistence transaction.
Partial/unknown/expired execution blocks the run. Stop preserves in-flight facts
and prevents new claims; it sends no zero setpoints. Neither stop nor later good
feedback releases a resynchronization latch. Recovery now requires the explicit
two-step admission described below.

The shared message namespace, additive SQLite schema, strict wire exports and
transaction rollback remain in force. Do not mix old/new executables on active
sessions. See [execution guide](../../../../docs/guides/Field_Execution.md) and
[ADR 0065](../../../../docs/architecture/decisions/0065-field-execution-feedback.md)
for compatibility, timing, energy-evidence and rollback limits.

```text
python -m pytest tests/workflows/test_field_execution.py tests/integration/service/test_field_execution_http.py
```

## Explicit recovery increment

`api.PrepareResynchronization` prepares immutable measured-state re-admission while
remaining blocked. `api.ResumeRun` recomputes and atomically commits it only if
current facts, control revision, authority and validity still match. Claimed sends
need complete command settlements and a new authority epoch with verified quiescence
through `api.RecoveryVerifier`. Default assembly does not trust unverified closure.

Recovery keeps original deadlines, failed cycle records and immutable initialization.
It never manufactures a completed cycle or retroactive safety guarantee. Completed
energy tasks retain their confirmed outcome; unproven past tasks block recovery.
Post-fence old actions re-block the run. Default shadow mode and the synchronous
software timing/model limits remain intact; no D5000 adaptation is implemented.

See [recovery guide](../../../../docs/guides/Field_Recovery.md) and
[ADR 0066](../../../../docs/architecture/decisions/0066-field-resynchronization.md).

```text
python -m pytest tests/workflows/test_field_recovery.py tests/integration/service/test_field_recovery_http.py
```

## Bounded timing increment

An explicitly admitted `bounded_monotone_v1` profile supports nonzero observation,
computation and transport delays, per-device application windows and skew, and
monotone exact settling before the next minute. Storage remains immediate ZOH.
Held-setpoint evidence and settled measured boundaries are required. The existing
minute alpha model only generates proposals. Independent interval reachability
checks all intermediate device orderings, energy switch-time extrema, forecast
bounds, original target/task deadlines and the finite terminal continuation.

Preparation and recovery can commit during the admitted preparation window.
Dispatch, delayed receipts and actual application times have separate guards.
Confirmed state remains measured; immutable batches store the timed certificate,
and the confirmed boundary retains a continuation for subsequent revalidation.
The original synchronous/default shadow profiles remain available. Exact clocks
and measurements, a validated monotone response model and configured local
fallback are explicit limitations. No D5000 protocol or background scheduler is
included. See [timing guide](../../../../docs/guides/Field_Bounded_Timing.md) and
[ADR 0067](../../../../docs/architecture/decisions/0067-field-bounded-timing.md).

```text
python -m pytest tests/workflows/test_field_timing.py tests/integration/service/test_field_timing_http.py
```

## Bounded observation increment

The opt-in `bounded_uncertain_v1` / `bounded_observation_v1` pair admits independently
approved deterministic measurement, timestamp and settled-response bounds. Trusted
port capabilities bind the full timing and metrology content with `api.policy_digest`.
Raw samples, estimates and clock provenance remain immutable. Continuous held-setpoint
history establishes stable boundary alignment; complete application facts and physical
reachability condition the confirmed state set. No safety or target clipping is used.

Preparation, target/planning updates and recovery propagate whole state boxes. AC v2
also certifies an independently admitted root-voltage input range. Query exposes
`confirmed_state_box`, labels the representative separately, and returns no exact
`confirmed_state`; energy outcomes can be `indeterminate`. Legacy exact profiles retain
the limitations described above. The trusted service UTC, finite response/forecast
assumptions and lack of online metrology-policy replacement remain explicit.

See [observation guide](../../../../docs/guides/Field_Observation_Uncertainty.md) and
[ADR 0068](../../../../docs/architecture/decisions/0068-field-observation-uncertainty.md).

```text
python -m pytest tests/workflows/test_field_uncertainty.py tests/integration/service/test_field_uncertainty_http.py tests/unit/power_flow/test_ac_envelope.py
```
