"""Recorded inputs and replay verification for the fixed patent-two example."""
from typing import Literal
from pydantic import BaseModel, ConfigDict, Field

Patent2Scenario = Literal["normal", "low_energy"]


class StudyValue(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)


class Patent2RunRequest(StudyValue):
    scenario: Patent2Scenario


class Patent2Branch(StudyValue):
    name: str
    parent: str
    child: str
    r_pu: float
    x_pu: float
    s_max_mva: float
    tap_ratio: float


class Patent2Region(StudyValue):
    name: str
    buses: tuple[str, ...]
    branches: tuple[Patent2Branch, ...]
    initial_energy_mwh: float
    minimum_energy_mwh: float
    maximum_energy_mwh: float
    storage_power_mw: float
    eta_charge: float
    eta_discharge: float
    load_p_mw: dict[str, tuple[float, ...]]
    load_q_mvar: dict[str, tuple[float, ...]]
    wind_available_mw: dict[str, tuple[float, ...]]
    pv_available_mw: dict[str, tuple[float, ...]]
    initial_p_mw: dict[str, float]
    initial_q_mvar: dict[str, float]


class Patent2Inputs(StudyValue):
    version: Literal["patent2-example-v1"] = "patent2-example-v1"
    scenario: Patent2Scenario
    duration_minutes: Literal[60] = 60
    source_start_minute: Literal[0] = 0
    price_cny_per_mwh: tuple[float, ...]
    cluster_limit_mw: float
    regions: tuple[Patent2Region, ...]
    input_sha256: str
    input_snapshot_json: str


class Patent2Evidence(StudyValue):
    inputs: Patent2Inputs
    replayed_minutes: int | None = Field(default=None, ge=0)
    replay_error: str = ""
    code_sha256: str
    code_snapshot_phase: Literal['before_run', 'after_run'] = 'after_run'
