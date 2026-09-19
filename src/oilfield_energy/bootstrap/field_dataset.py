"""Construct field dataset adapters; no work is performed during import."""

from oilfield_energy.modules.measurements.adapters.local_files import LocalDatasetReader
from oilfield_energy.modules.measurements.adapters.manifest_json import JsonManifestDecoder
from oilfield_energy.modules.measurements.api import CaptureDataset
from oilfield_energy.workflows.field_dataset.adapters.result_directory import DirectoryResultStore
from oilfield_energy.workflows.field_dataset.api import FieldDatasetWorkflow

from .adapters.legacy_snapshot import LegacySnapshotEngine
from .adapters.runtime_provenance import runtime_artifact


def create_field_dataset_workflow() -> FieldDatasetWorkflow:
    return FieldDatasetWorkflow(
        CaptureDataset(LocalDatasetReader(), JsonManifestDecoder()),
        LegacySnapshotEngine(),
        DirectoryResultStore(),
        provenance=(runtime_artifact(),),
    )
