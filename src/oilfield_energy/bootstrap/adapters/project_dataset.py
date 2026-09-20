"""Load the packaged authority and project it to legacy numerical inputs.

I/O is confined to this composition boundary. Historical captured bundles are
decoded independently; they are never replaced by the current package resource.
"""

import json
from dataclasses import replace
from hashlib import sha256
from importlib.resources import files
from typing import Literal

import numpy as np
from pydantic import TypeAdapter

from oilfield_energy.data import ProjectCase
from oilfield_energy.modules.control.contracts import PlantInputs
from oilfield_energy.modules.studies.api import generate_inputs
from oilfield_energy.modules.studies.contracts import StudySpec, UnifiedDataset
from oilfield_energy.network_model import NetworkModelV2

from .simulation_case import make_case
from .simulation_files import validate_json


def load_dataset() -> tuple[UnifiedDataset, str]:
    raw = files("oilfield_energy.bootstrap").joinpath("assets/unified_dataset.json").read_bytes()
    return UnifiedDataset.model_validate_json(validate_json(raw), strict=True), sha256(
        raw
    ).hexdigest()


def study_recipe(region: str = "SC") -> StudySpec:
    dataset, _ = load_dataset()
    for spec in dataset.regions:
        if spec.region_id == region:
            return spec
    raise ValueError(f"unknown dataset region: {region}")


def plant_inputs_by_region() -> dict[str, PlantInputs]:
    dataset, _ = load_dataset()
    return {spec.region_id: generate_inputs(spec)[2] for spec in dataset.regions}


def build_synthetic_case(
    steps: int = 96, *, profile_kind: Literal["day_ahead", "intraday"] = "day_ahead"
) -> ProjectCase:
    """Compatibility name for the sole packaged dataset; full-day resampling."""
    if type(steps) is not int or not 1 <= steps <= 288:
        raise ValueError("steps must be an integer in 1..288")
    if profile_kind not in {"day_ahead", "intraday"}:
        raise ValueError("optimization profile must be day_ahead or intraday")
    dataset, digest = load_dataset()
    cases: list[ProjectCase] = []
    for spec in dataset.regions:
        network = TypeAdapter(NetworkModelV2).validate_json(
            json.dumps(
                {
                    **spec.network,
                    "operating_modes": [
                        {"mode_id": "normal", "description": "synthetic normal mode"}
                    ],
                    "default_operating_mode_id": "normal",
                },
                allow_nan=False,
            ),
            strict=True,
        )
        profiles = generate_inputs(spec)
        cases.append(make_case(spec, network, profiles[0 if profile_kind == "day_ahead" else 1]))
    original = cases[0]
    hours = np.arange(steps, dtype=float) * (24.0 / steps)

    def sample(values: np.ndarray) -> np.ndarray:
        return np.interp(hours, original.time_hours, values, period=24.0)

    grids = [
        replace(
            case.microgrids[0],
            **{
                name: {
                    bus: sample(values) for bus, values in getattr(case.microgrids[0], name).items()
                }
                for name in ("load_p_mw", "load_q_mvar", "wind_available_mw", "pv_available_mw")
            },
        )
        for case in cases
    ]
    # Tariff is a step function, never interpolate across a price boundary.
    indices = np.minimum((hours * 4).astype(int), 95)
    return replace(
        original,
        microgrids=grids,
        time_hours=hours,
        price_cny_per_mwh=np.array(original.price_cny_per_mwh[indices], dtype=float),
        assumptions=replace(original.assumptions, dt_hours=24.0 / steps),
        cluster_import_limit_mw=dataset.cluster_import_limit_mw,
        dataset_id=dataset.dataset_id,
        dataset_revision=dataset.revision,
        dataset_sha256=digest,
        profile_kind=profile_kind,
    )
