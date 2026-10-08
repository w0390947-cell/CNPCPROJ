"""Compose the synthetic input generator, independent of any execution algorithm."""

from pathlib import Path

from oilfield_energy.workflows.field_dataset.adapters.result_directory import DirectoryResultStore

from .adapters.project_dataset import study_recipe
from .adapters.simulation_files import generate_dataset


def generate_study_inputs(config: Path | None, output: Path) -> None:
    generate_dataset(study_recipe() if config is None else config, output, DirectoryResultStore())
