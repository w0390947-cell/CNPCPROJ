"""Protect the disclosure demo boundary without asserting solver-specific trajectories."""
from hashlib import sha256
import json
from types import SimpleNamespace
import asyncio

import numpy as np
import pytest
from fastapi import FastAPI

from oilfield_energy.bootstrap.adapters import patent2_example as example
from oilfield_energy.bootstrap.adapters.project_dataset import build_synthetic_case
from oilfield_energy.service import SimulationRequest, run_simulation
from oilfield_energy.web_api import router, _jobs


def fixed(**changes):
    values = dict(scenario_type='cluster_coordination', steps=4,
                  patent2_case='normal', admm_max_iterations=1000)
    values.update(changes)
    return SimulationRequest(**values)


def test_fixed_inputs_preserve_native_clock_and_only_energy_differs():
    normal = example.describe_example('normal')
    low = example.describe_example('low_energy')
    base = build_synthetic_case(96)
    assert normal.input_sha256 == sha256(normal.input_snapshot_json.encode()).hexdigest()
    assert normal.input_sha256 != low.input_sha256
    assert [r.initial_energy_mwh for r in normal.regions] == [2.5, 2.0, 1.8]
    assert [r.initial_energy_mwh for r in low.regions] == pytest.approx([.5, .4, .36])
    for a, b, original in zip(normal.regions, low.regions, base.microgrids, strict=True):
        assert a.model_dump(exclude={'initial_energy_mwh'}) == b.model_dump(exclude={'initial_energy_mwh'})
        for bus, values in a.load_p_mw.items():
            np.testing.assert_array_equal(values, original.load_p_mw[bus][:4])
        assert len(a.branches) == len(a.buses) - 1


@pytest.mark.parametrize('change', [
    {'steps': 96}, {'storage_enabled': False}, {'scenario_type': 'single_microgrid'},
    {'admm_max_iterations': 220}, {'solver': {'time_limit_seconds': 30}},
    {'solver': {'quality_policy': 'quality-first-v1'}}, {'patent2_case': 'anything'},
])
def test_fixed_example_rejects_incompatible_options(change):
    with pytest.raises(ValueError):
        fixed(**change)


def test_service_passes_explicit_study_instead_of_generic_four_point_day(monkeypatch):
    import oilfield_energy.service as service
    captured = {}
    live_events = []
    class Result:
        cluster_execution = None
        def model_copy(self, *, update):
            captured.update(update)
            return self
    def run(request, progress, *, communication_fault, input_case, live_callback=None):
        assert not communication_fault
        assert live_callback is not None
        live_callback({'kind': 'test_callback_forwarded'})
        np.testing.assert_array_equal(input_case.time_hours, [0, .25, .5, .75])
        assert [m.storage.e_initial_mwh for m in input_case.microgrids] == pytest.approx([.5, .4, .36])
        return Result()
    monkeypatch.setattr(service, '_run_cluster', run)
    run_simulation(fixed(patent2_case='low_energy'), live_callback=live_events.append)
    assert live_events[0]['kind'] == 'cluster_study'
    assert live_events[0]['inputs']['scenario'] == 'low_energy'
    from oilfield_energy.bootstrap.adapters.cluster_live import ClusterLiveEvent
    ClusterLiveEvent.model_validate(live_events[0] | {'sequence': 1})
    assert live_events[1:] == [{'kind': 'test_callback_forwarded'}]
    evidence = captured['patent2_evidence']
    assert evidence.inputs.scenario == 'low_energy'
    assert evidence.replayed_minutes is None
    assert evidence.replay_error
    with pytest.raises(ValueError):
        run_simulation(fixed(), input_case=example.build_example('normal'))


def test_replay_failure_is_recorded_without_claiming_success(monkeypatch):
    class Safety:
        initial_continuation = object()
        def model_dump_json(self):
            return '{}'
        @classmethod
        def model_validate_json(cls, raw):
            return cls()
    def fail(_):
        raise ValueError('certificate mismatch')
    monkeypatch.setattr(example, 'replay_cluster_safety', fail)
    result = example.finish_example(example.describe_example('normal'), SimpleNamespace(safety=Safety()))
    assert result.replayed_minutes is None
    assert result.replay_error == 'certificate mismatch'


def test_http_preset_has_no_hidden_user_overrides(tmp_path, monkeypatch):
    from oilfield_energy.bootstrap.adapters import patent2_library
    monkeypatch.setattr(patent2_library, 'LIBRARY_ROOT', tmp_path)
    captured=[]
    class Jobs:
        def create(self, request):
            captured.append(request)
            return dict(simulation_id='a'*32, name=request.name, state='queued',
                        created_at='2026-11-05T00:00:00Z', updated_at='2026-11-05T00:00:00Z')
        def recent_patent2_results(self):
            return []
    app=FastAPI()
    app.include_router(router)
    app.dependency_overrides[_jobs]=lambda:Jobs()
    async def call(method, path, body=None, query=b''):
        messages=[]
        async def receive():
            return {'type':'http.request','body':json.dumps(body).encode() if body else b'','more_body':False}
        async def send(message):
            messages.append(message)
        await app({'type':'http','method':method,'path':path,'query_string':query,
                   'headers':[(b'content-type',b'application/json')],'scheme':'http','server':('test',80),
                   'client':('test',1),'root_path':'','http_version':'1.1'},receive,send)
        return next(m['status'] for m in messages if m['type']=='http.response.start'), json.loads(b''.join(m.get('body',b'') for m in messages if m['type']=='http.response.body'))
    async def check():
        code, response=await call('POST','/api/patent2/runs',{'scenario':'low_energy'})
        assert code == 202
        request=captured[0]
        assert request.patent2_case == 'low_energy'
        assert request.steps == 4 and request.admm_max_iterations == 1000
        assert request.solver.time_limit_seconds == 60
        assert request.solver.quality_policy is None
        assert (await call('POST','/api/patent2/runs',{'scenario':'normal','steps':96}))[0] == 422
        assert (await call('GET','/api/patent2/inputs',query=b'scenario=wrong'))[0] == 422
        assert (await call('GET','/api/patent2/runs'))[1] == []
        assert (await call('GET','/api/patent2/inputs'))[1]['scenario'] == 'normal'
        assert (await call('GET','/api/patent2/library'))[1] == []
        assert (await call('GET','/api/patent2/library/normal'))[0] == 404
        assert (await call('GET','/api/patent2/library/wrong'))[0] == 422
        (tmp_path/'normal.json').write_text('{}',encoding='utf8')
        assert (await call('GET','/api/patent2/library'))[1] == ['normal']
        assert (await call('GET','/api/patent2/library/normal'))[0] == 422
    asyncio.run(check())


def test_recent_patent_results_exclude_generic_cluster_and_failed_jobs(tmp_path):
    from oilfield_energy.job_manager import SimulationJobManager, JobStatus, write_job_status
    manager=SimulationJobManager(tmp_path/'jobs')
    try:
        for i, (preset, state) in enumerate([('normal','succeeded'), ('low_energy','succeeded'), (None,'succeeded'), ('normal','failed')]):
            directory=manager.root/f'{i:032x}'
            directory.mkdir()
            write_job_status(directory, JobStatus(simulation_id=directory.name, name='test', state=state,
                created_at=f'2026-11-05T00:0{i}:00Z', updated_at=f'2026-11-05T00:0{i}:00Z', result_available=state=='succeeded'))
            (directory/'request.json').write_text(json.dumps(fixed().model_dump(mode='json') | {'patent2_case':preset}),encoding='utf8')
            (directory/'result.json').write_text('{}',encoding='utf8')
        assert [h.simulation_id for h in manager.recent_patent2_results()] == [f'{1:032x}',f'{0:032x}']
        assert manager.recent_single_results() == []
    finally:
        manager.close()
