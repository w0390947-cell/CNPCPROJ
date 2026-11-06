"""Input identity belongs to the run, including live, failed and historical records."""
from hashlib import sha256
import json
from types import SimpleNamespace

import pytest

from oilfield_energy.bootstrap.adapters.project_dataset import build_synthetic_case
from oilfield_energy.bootstrap.adapters.single_planning import single_execution_policy
from oilfield_energy.bootstrap.adapters.single_provenance import capture_single_inputs
from oilfield_energy.workflows.single_execution.contracts import SingleExecution, UnifiedExecution
from oilfield_energy.workflows.single_execution.contracts_provenance import SingleInputRecord


def test_input_capture_freezes_values_and_detects_snapshot_tampering():
    case = build_synthetic_case(8)
    request = {'solver': {'time_limit_seconds': 60}}
    record = capture_single_inputs(case, single_execution_policy(None), {'request': request}).record
    request['solver']['time_limit_seconds'] = 90
    snapshot = json.loads(record.input_snapshot_json)
    assert snapshot['request']['solver']['time_limit_seconds'] == 60
    assert snapshot['reference_case_after_events']['dataset_sha256'] == record.dataset_sha256
    assert record.input_sha256 != record.dataset_sha256
    assert record.code_snapshot_phase == 'before_run'
    assert record.runtime_versions['python']
    assert record.runtime_versions['pyscipopt']
    assert record.snapshot_scope == 'reference_only'
    SingleInputRecord.model_validate_json(record.model_dump_json())
    with pytest.raises(ValueError, match='SHA-256 mismatch'):
        SingleInputRecord.model_validate({**record.model_dump(), 'input_snapshot_json': record.input_snapshot_json + ' '})


def test_live_and_final_record_share_complete_pre_execution_inputs(monkeypatch, tmp_path):
    from oilfield_energy.bootstrap.adapters import single_unified
    from oilfield_energy.live_journal import LiveJournal, read_live
    case = build_synthetic_case(8)
    policy = single_execution_policy(None)
    capture = capture_single_inputs(case, policy, {'request': {'region': 'SC'}})
    captured = {}
    reference = SimpleNamespace(microgrids={'SC': {'storage_energy_mwh': [2.5] * 9}})

    def feedback(domain, initial, **kwargs):
        # Exercise recording around the controller boundary without a full-day solve.
        captured.update(domain=domain, initial=initial)
        kwargs['live']({'kind': 'initial'})
        return UnifiedExecution(
            domain=domain, initial=initial, status='unproven',
            requested_steps=kwargs['total_steps'], update_steps=kwargs['update_steps'],
            tracking_status='not_assessed', terminal_proof_digest='',
            hard_task={'guarantee': 'unproven', 'outcome': 'pending'},
        )
    monkeypatch.setattr(single_unified, 'run_unified_feedback', feedback)
    result = single_unified.run_unified_single(
        case, reference, 'SC', storage_enabled=True, time_limit_seconds=60,
        packaged_inputs=True, events=[], policy=policy, input_capture=capture,
        live=LiveJournal(tmp_path).publish,
    )
    record = result.input_record
    assert record is not None
    assert read_live(tmp_path)['events'][0]['input_record'] == record.model_dump(mode='json')
    assert record.code_sha256 == capture.record.code_sha256
    assert record.recorded_at == capture.record.recorded_at
    assert record.snapshot_scope == 'reference_and_execution'
    snapshot = json.loads(record.input_snapshot_json)
    inputs = snapshot['execution_inputs']
    assert inputs['domain'] == captured['domain'].model_dump(mode='json')
    assert inputs['initial_state'] == captured['initial'].model_dump(mode='json')
    assert inputs['input_forecast'] == result.input_forecast.model_dump(mode='json')
    assert inputs['reference_storage_energy_mwh'] == [2.5] * 9
    assert inputs['total_steps'] == 1440
    assert inputs['intraday_case']['time_hours']
    assert inputs['plant_inputs']['load_p']
    assert record.input_sha256 == sha256(record.input_snapshot_json.encode()).hexdigest()
    restored = SingleExecution.model_validate_json(result.model_dump_json())
    assert restored.input_record == record
    old = result.model_dump(mode='json')
    del old['input_record']
    assert SingleExecution.model_validate_json(json.dumps(old)).input_record is None


def test_service_captures_request_and_code_before_first_solve(monkeypatch):
    from oilfield_energy import service
    from oilfield_energy.bootstrap.adapters import single_provenance
    records = []
    capture = single_provenance.capture_single_inputs
    def save(*args, **kwargs):
        captured = capture(*args, **kwargs)
        records.append(captured.record)
        return captured
    class ReachedSolver(Exception):
        pass
    def solve(*args, **kwargs):
        assert len(records) == 1
        raise ReachedSolver()
    monkeypatch.setattr(single_provenance, 'capture_single_inputs', save)
    monkeypatch.setattr(service, '_solve', solve)
    request = service.SimulationRequest(steps=8)
    with pytest.raises(ReachedSolver):
        service.run_simulation(request)
    snapshot = json.loads(records[0].input_snapshot_json)
    assert snapshot['request'] == request.model_dump(mode='json')
    assert snapshot['packaged_inputs'] is True
    assert snapshot['baseline_planning_case']['microgrids']
    assert snapshot['optimized_planning_case']['microgrids']
    assert records[0].code_snapshot_phase == 'before_run'
