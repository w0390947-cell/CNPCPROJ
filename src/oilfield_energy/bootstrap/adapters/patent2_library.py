"""Durable local demo records, separate from the temporary job retention store."""
from hashlib import sha256
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from oilfield_energy.service import SimulationResult

from oilfield_energy.modules.studies.contracts_patent2 import Patent2Scenario

LIBRARY_ROOT = Path(__file__).resolve().parents[4] / 'results' / 'patent2_examples'


def library_path(scenario: Patent2Scenario) -> Path:
    if scenario not in ('normal', 'low_energy'):
        raise ValueError('unsupported patent2 scenario')
    return LIBRARY_ROOT / f'{scenario}.json'


def read_library(scenario: Patent2Scenario) -> 'SimulationResult':
    # Lazy import avoids the service -> example adapter dependency cycle.
    from oilfield_energy.service import SimulationResult
    result = SimulationResult.model_validate_json(library_path(scenario).read_bytes())
    evidence = result.patent2_evidence
    if (evidence is None or result.cluster_execution is None
            or evidence.inputs.scenario != scenario or result.request.patent2_case != scenario
            or sha256(evidence.inputs.input_snapshot_json.encode()).hexdigest() != evidence.inputs.input_sha256):
        raise ValueError('saved patent2 record has inconsistent identity or input digest')
    return result
