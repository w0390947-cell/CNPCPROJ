"""Durable, complete fixed single-grid examples, independent of job retention."""
from pathlib import Path
from typing import TYPE_CHECKING

from .single_example import describe_single_example

if TYPE_CHECKING:
    from oilfield_energy.service import SimulationResult

LIBRARY_ROOT = Path(__file__).resolve().parents[4] / "results" / "single_examples"


def library_path() -> Path:
    return LIBRARY_ROOT / "core.json"


def validate_example(result: "SimulationResult") -> None:
    """Check identity and completeness, never turn research failures into passes."""
    request, metadata, execution = result.request, result.metadata, result.single_execution
    if (
        request.scenario_type != "single_microgrid"
        or metadata.scenario_type != "single_microgrid"
        or request.region != "SC" or metadata.region != "SC"
        or request.steps != 8 or metadata.steps != 8 or metadata.dt_hours != 3
        or not request.storage_enabled or request.events
        or request.single_boundary is not None or request.single_research is not None
        or execution is None or execution.unified is None
        or execution.total_minutes != 1440 or execution.completed_minutes != 1440
    ):
        raise ValueError("saved result is not the complete fixed single-grid example")
    u = execution.unified
    expected = describe_single_example()
    # Context digests retain their historical provenance. Compare the complete
    # physical/forecast inputs, not a digest whose serialization can change.
    exclude = {"context_id": True, "devices": {"context_id"}}
    if (
        u.requested_steps != expected.total or len(u.steps) != expected.total
        or u.update_steps != expected.update_steps
        or u.domain.model_dump(exclude=exclude) != expected.domain.model_dump(exclude=exclude)
        or u.domain.context_id != u.domain.devices.context_id
        or u.initial != expected.initial
        or metadata.dataset_sha256 != expected.dataset_sha256
        or any(row.step != i for i, row in enumerate(u.steps))
    ):
        raise ValueError("saved single-grid inputs or execution sequence differ from the fixed example")


def read_library() -> "SimulationResult":
    from oilfield_energy.service import SimulationResult
    result = SimulationResult.model_validate_json(library_path().read_bytes())
    validate_example(result)
    return result
