# Technical runtime

Owner: 项目技术负责人; reviewer: 设备控制维护负责人.

`OwnedProcessPool` (`parallel.py`) owns a bounded spawn pool for one job, returns
results in input order and propagates failures. Each worker watches its parent's
sentinel and caps loaded native numerical pools to one thread per task through
the declared `threadpoolctl` dependency. Callables must be picklable, numerical
backends imported before task execution, and native long calls must release the
GIL for the owner watcher to run. It imports no business or solver modules.
Use it as a context manager; one worker runs inline for reference execution.
Tests: `tests/integration/dispatch/test_parallel_regions.py`; see ADR 0024.

`PeriodicWorker` invokes an injected callback on one thread, stops on exceptions,
and exposes failure to its owner. It knows nothing about voltage or device state.

`JobRootLease` owns a local-filesystem coordinator lock until close/process exit.
`watch_owner_pipe` is an opt-in managed-worker entrypoint guard: EOF on its
exclusive stdin pipe ends that child process. It must not be armed in an API
server or an interactive process. Neither utility imports business models.
On Windows, arming the guard first duplicates the owner pipe into a private,
non-inheritable descriptor and redirects both Python stdin and the Windows
standard-input handle to NUL. This happens before the watcher reads: nested
spawned interpreters must never initialize against a pipe with a pending read.
EOF/unexpected data still revoke the managed worker and its initialized regional
descendants. See ADR 0026 and `tests/integration/service/test_managed_parallel.py`.
Publication ownership and compatibility are defined in
`docs/architecture/decisions/0012-job-publication-ownership.md`.

`RetentionPolicy` (`retention.py`) owns pure elapsed-time/capacity rules with
validated immutable defaults: 24 hours, 5,000,000,000 bytes, 60-second checks.
`RetentionStore` (`retention_store.py`) measures complete job trees and quarantines
them before erasure. It only receives terminal-time and locked-retirement callbacks;
it never imports `JobStatus`, simulation services, or solver models. The caller must
hold the coordinator lease and serialize maintenance with shutdown. `.retired`
is reserved for crash-recoverable erasures; partial failures remain charged and retried.
The journal rotates at 256 KiB with two backups. These utilities use only runtime
helpers, the standard library and Pydantic configuration validation.

Legacy Python callers should use `SimulationJobManager.read_result()` to obtain a
complete snapshot protected from cleanup; a returned filesystem path is not a lease.
Storage scope, admission failures, rollback limits and historical compatibility are
defined in `docs/architecture/decisions/0013-job-result-retention.md`.

Tests: `tests/unit/runtime/test_retention.py` and
`tests/integration/service/test_job_retention.py`, plus the existing job-lifecycle tests.
