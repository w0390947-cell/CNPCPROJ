"""Explicit simulation composition root."""

from pathlib import Path

from oilfield_energy.modules.measurements.adapters.local_files import LocalDatasetReader
from oilfield_energy.modules.measurements.adapters.manifest_json import JsonManifestDecoder
from oilfield_energy.modules.measurements.api import CaptureDataset
from oilfield_energy.workflows.field_dataset.adapters.result_directory import DirectoryResultStore
from oilfield_energy.workflows.shancheng_simulation.api import ShanchengSimulation

from .adapters.project_dataset import study_recipe
from .adapters.runtime_provenance import runtime_artifact
from .adapters.simulation_files import generate_dataset
from .adapters.simulation_runner import SimulationEngine


def create_simulation() -> ShanchengSimulation:
    return ShanchengSimulation(
        CaptureDataset(LocalDatasetReader(), JsonManifestDecoder()),
        SimulationEngine(),
        DirectoryResultStore(),
        (
            runtime_artifact(
                solver="SCIP MISOCP, independent AC, device SIL; seeds in simulation_parameters.json"
            ),
        ),
    )


def generate_simulation(config: Path | None, output: Path) -> None:
    generate_dataset(study_recipe() if config is None else config, output, DirectoryResultStore())
