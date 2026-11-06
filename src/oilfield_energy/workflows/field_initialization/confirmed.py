"""Read the separately committed field boundary without rewriting initialization."""

from oilfield_energy.modules.control.api import point_box
from oilfield_energy.modules.control.contracts import TubeBox, TubeState

from .contracts import InitializationEvidence, InitializeRequest
from .contracts_values import Snapshot, SnapshotData
from .validation import require


def confirmed_snapshot(current: InitializationEvidence) -> SnapshotData:
    return (
        current.confirmed_boundary.snapshot
        if current.confirmed_boundary
        else current.request.payload.initial_snapshot
    )


def confirmed_state(current: InitializationEvidence) -> TubeState:
    return (
        current.confirmed_boundary.state
        if current.confirmed_boundary
        else current.initial
    )


def confirmed_box(current: InitializationEvidence) -> TubeBox:
    if current.confirmed_boundary:
        return current.confirmed_boundary.state_box or point_box(
            current.confirmed_boundary.state
        )
    return current.initial_box or point_box(current.initial)


def confirmed_step(current: InitializationEvidence) -> int:
    seconds = (
        confirmed_snapshot(current).snapshot_at
        - current.request.payload.planning_inputs.effective_from
    ).total_seconds()
    require(
        seconds >= 0 and seconds % 60 == 0,
        "CONFIRMED_STATE_REQUIRED",
        "Confirmed boundary is not on the active forecast clock",
    )
    return int(seconds // 60)


def confirmed_request(current: InitializationEvidence) -> InitializeRequest:
    p = current.request.payload
    s = Snapshot.model_validate(
        confirmed_snapshot(current).model_dump() | {"context_id": None}
    )
    return current.request.model_copy(
        update={"payload": p.model_copy(update={"initial_snapshot": s})}
    )
