"""Assemble shadow field-session use cases; constructing them performs no I/O."""

from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path

from oilfield_energy.workflows.field_initialization.adapters.cycle_storage import (
    SqliteControlCycles,
)
from oilfield_energy.workflows.field_initialization.adapters.execution_storage import (
    SqliteExecution,
)
from oilfield_energy.workflows.field_initialization.adapters.planning_storage import (
    SqlitePlanningUpdates,
)
from oilfield_energy.workflows.field_initialization.adapters.snapshot_storage import (
    SqliteSnapshots,
)
from oilfield_energy.workflows.field_initialization.adapters.storage import (
    LocalArtifacts,
    SqliteSessions,
)
from oilfield_energy.workflows.field_initialization.adapters.target_storage import (
    SqlitePccTargets,
)
from oilfield_energy.workflows.field_initialization.api import (
    DispatchControlCycle,
    ExecutionTransport,
    InitializeRun,
    PrepareControlCycle,
    PrepareResynchronization,
    QueryRunStatus,
    RecoveryVerifier,
    ReportExecution,
    RequestPccTarget,
    ResumeRun,
    StopRun,
    SubmitSnapshot,
    UpdatePlanningInputs,
)

from .adapters.field_initialization_network import FieldNetworkAssembler


def create_field_initializer(
    root: Path,
    *,
    clock: Callable[[], datetime] | None = None,
    transport: ExecutionTransport | None = None,
) -> InitializeRun:
    return InitializeRun(
        SqliteSessions(root / "sessions.sqlite3"),
        FieldNetworkAssembler(),
        LocalArtifacts(root / "objects"),
        clock or (lambda: datetime.now(timezone.utc)),
        transport=transport,
    )


def create_planning_updater(
    root: Path, *, clock: Callable[[], datetime] | None = None
) -> UpdatePlanningInputs:
    return UpdatePlanningInputs(
        SqlitePlanningUpdates(root / "sessions.sqlite3"),
        clock or (lambda: datetime.now(timezone.utc)),
    )


def create_pcc_target_requester(
    root: Path, *, clock: Callable[[], datetime] | None = None
) -> RequestPccTarget:
    return RequestPccTarget(
        SqlitePccTargets(root / "sessions.sqlite3"),
        clock or (lambda: datetime.now(timezone.utc)),
    )


def create_snapshot_submitter(
    root: Path, *, clock: Callable[[], datetime] | None = None
) -> SubmitSnapshot:
    return SubmitSnapshot(
        SqliteSnapshots(root / "sessions.sqlite3"),
        clock or (lambda: datetime.now(timezone.utc)),
    )


def create_control_cycle_preparer(
    root: Path,
    *,
    clock: Callable[[], datetime] | None = None,
    transport: ExecutionTransport | None = None,
) -> PrepareControlCycle:
    return PrepareControlCycle(
        SqliteControlCycles(root / "sessions.sqlite3"),
        clock or (lambda: datetime.now(timezone.utc)),
        transport=transport,
    )


def create_execution_services(
    root: Path,
    *,
    clock: Callable[[], datetime] | None = None,
    transport: ExecutionTransport | None = None,
) -> tuple[ReportExecution, QueryRunStatus, StopRun, DispatchControlCycle]:
    store = SqliteExecution(root / "sessions.sqlite3")
    clock = clock or (lambda: datetime.now(timezone.utc))
    return (
        ReportExecution(store, clock),
        QueryRunStatus(store, clock, transport),
        StopRun(store, clock, transport),
        DispatchControlCycle(store, clock, transport),
    )


def create_recovery_services(
    root: Path,
    *,
    clock: Callable[[], datetime] | None = None,
    transport: ExecutionTransport | None = None,
    verifier: RecoveryVerifier | None = None,
) -> tuple[PrepareResynchronization, ResumeRun]:
    store = SqliteExecution(root / "sessions.sqlite3")
    clock = clock or (lambda: datetime.now(timezone.utc))
    return (
        PrepareResynchronization(store, clock, transport, verifier),
        ResumeRun(store, clock, transport, verifier),
    )
