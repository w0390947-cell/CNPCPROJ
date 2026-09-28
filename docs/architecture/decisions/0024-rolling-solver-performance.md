# ADR 0024: Reuse rolling numerical models and isolate regional parallel solves

Date: 2026-09-25. Status: accepted for this implementation.
Owners: 优化调度维护负责人、仿真研究维护负责人.
Review responsibilities: 项目技术负责人、设备控制维护负责人.

## Problem

The complete 96-window cluster run repeatedly discovered installed CVXPY solvers,
rebuilt three regional convex models plus the cluster projection, and solved three
independent regional device plans serially. The same-input four-window diagnostic
measured 2164 discovery calls taking 14.55 of 40.02 seconds without cProfile.
The preceding failed run had stopped before actual rolling execution; its nine-second
duration was not a valid full-day performance baseline.

## Decision and boundaries

- Discover installed convex solvers lazily once per interpreter, retaining the
  existing preferred/backend-fallback order and error behavior. Installing or removing
  backends requires a new worker. No optimization result is cached globally.
- `CoordinationWorkspace` belongs to one rolling engine and retains only its latest
  compatible models. It is not thread-safe and must not be shared by jobs. Legacy
  `regional_control.py` keeps CVXPY expressions; new public dispatch contracts remain
  independent of numerical SDKs, as in ADR 0023 and GOV-04.
- Parameters refresh load plus calibrated losses, individual renewable bounds,
  price, SOC initial/terminal energies and regional/projection safety floors.
  DPP is enforced for regional autonomous/coordinated and projection models.
- Structural identity contains the selected case's static dataclass fields, device
  mappings, networks, input revision/digest, assumptions, configuration, capabilities,
  region order and horizon size. Only the explicitly refreshed data is excluded.
  The private pickle bytes are compared in memory, never loaded/deserialized or persisted.
  Structure changes rebuild; an invalid update clears the workspace. One retained
  model set bounds memory when the end-of-day horizon shrinks.
- Numerical solver caches may survive parameter updates. Each ADMM call still
  creates new duals, epochs, channel state, history and convergence certificates.
  Returned schedules own their arrays. Every window is solved and validated anew.
- Regional plan realization is a stateless, typed adapter operation. The owner
  sends serializable requests to up to three persistent spawned processes and
  collects results in original region order. No live solver or engine is shared.
  Aggregate capacity and all regional certificates are checked before adoption.
- Runtime owns process startup, ordered collection, exception propagation and
  shutdown. A parent-sentinel watcher stops spawned workers on owner death. SCIP
  uses its documented `optimizeNogil()` so this watcher can run during native solves;
  the underlying SCIP solve and mathematical limits are unchanged.
- Each region uses one SCIP thread. `threadpoolctl` caps loaded BLAS/OpenMP pools
  inside each spawned task after its backend module has been imported. This is a
  declared direct dependency in both installation manifests and the lockfile.
  Windows spawn avoids depending on fork behavior. Context exit waits for in-flight
  bounded solves and cancels queued tasks; forced owner termination revokes workers.
- `ExecutionPolicy` records worker count, native thread cap and model reuse. Absent
  historical fields retain serial/no-reuse defaults and an unknown native thread cap.
  New default runs request three workers and model reuse; an explicit single-worker
  policy remains available for reference execution. JSON/TypeScript are generated
  from the Python authority. No UI performance redesign is included.

The rolling workflow remains sequential across time: plan, execute, feed back, then
plan the next window. Capacity rules, tolerances, objective terms, forecast horizon,
minute dynamics, AC validation and failure evidence retain their existing meanings.

## Alternatives and tradeoffs

Caching full plans is unsafe because the observed initial energy and forecasts can
change. Sharing SCIP objects among threads would require unverified build/runtime
guarantees. Starting a new process per window would multiply spawn overhead. The
chosen pool costs startup time and memory, so short scenarios may gain little from
parallelism alone. Warming ADMM duals across windows is deliberately outside this
change because it changes algorithm initialization and communication evidence.

Cold/fresh and warm plans need not be bit-for-bit identical; numerical comparisons
use explicit tolerances and retain independent feasibility/engineering checks.
Performance is measured separately from profiled timing and after tests finish.

## Verification

`tests/integration/dispatch/test_coordination_workspace.py` covers updated window
inputs, static invalidation, bad-update recovery, solver fallback, snapshots and
cold/warm numerical agreement. `test_parallel_regions.py` exercises real serial and
parallel SCIP/AC plans, infeasibility, ordered isolated workers, failure propagation
and forced-owner-exit cleanup. Existing rolling failure and P/Q rejection tests
inject at the new batch boundary and still prove that no failed window executes.
Architecture, installed-package, generated contracts and full-day numerical/performance
checks accompany the implementation; measurements are in the task artifact directory.

## Primary references

- [CVXPY DPP](https://www.cvxpy.org/version/1.6/tutorial/dpp/index.html): parameter-to-data compilation reuse.
- [CVXPY solver features](https://www.cvxpy.org/tutorial/solvers/index.html): warm start and solver-dependent caching.
- [Python multiprocessing](https://docs.python.org/3/library/multiprocessing.html): spawn and parent process sentinel.
- [PySCIPOpt Model API](https://pyscipopt.readthedocs.io/en/latest/api/model.html): `optimizeNogil`.
