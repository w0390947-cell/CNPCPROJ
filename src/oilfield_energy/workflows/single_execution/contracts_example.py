"""Read-only input description for the fixed single-grid demonstration."""
from pydantic import BaseModel, ConfigDict
from .contracts_unified import UnifiedDomain
from oilfield_energy.modules.control.contracts import TubeState, PlantInputs


class SingleExampleInputs(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    region: str
    total: int
    update_steps: int
    reference_time_hours: tuple[float, ...]
    dataset_sha256: str
    dataset_id: str | None = None
    dataset_revision: str | None = None
    execution_version: str | None = None
    input_basis: str | None = None
    domain: UnifiedDomain
    initial: TubeState
    input_forecast: PlantInputs | None = None
