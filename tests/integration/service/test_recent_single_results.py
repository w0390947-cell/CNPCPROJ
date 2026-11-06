import json
import asyncio
from oilfield_energy.job_manager import JobStatus, SimulationJobManager, write_job_status
from oilfield_energy.service import SimulationRequest


def test_recent_candidates_are_ordered_and_exclude_other_tasks(tmp_path):
    manager = SimulationJobManager(tmp_path / 'jobs')
    try:
        def add(number, created, *, state='succeeded', region='SC', result=True, **request_changes):
            job_id = f'{number:032x}'
            directory = manager.root / job_id
            directory.mkdir()
            write_job_status(directory, JobStatus(simulation_id=job_id, name='test', state=state,
                created_at=created, updated_at=created, result_available=result and state == 'succeeded'))
            request = SimulationRequest(region=region).model_dump(mode='json')
            request.update(request_changes)
            (directory / 'request.json').write_text(json.dumps(request), encoding='utf8')
            if result:
                (directory / 'result.json').write_text('{}', encoding='utf8')
            return job_id

        older = add(1, '2026-11-03T10:00:00+08:00')
        newer = add(2, '2026-11-03T03:00:00+00:00')
        add(3, '2026-11-03T04:00:00+00:00', state='cancelled')
        add(4, '2026-11-03T05:00:00+00:00', region='YA_B')
        add(5, '2026-11-03T06:00:00+00:00', result=False)
        add(6, '2026-11-03T07:00:00+00:00', steps=24)
        add(7, '2026-11-03T08:00:00+00:00', single_research={'changed': True})
        assert [item.simulation_id for item in manager.recent_single_results()] == [newer, older]
    finally:
        manager.close()


def test_recent_route_resolves_before_job_id_route():
    from fastapi import FastAPI
    from oilfield_energy.web_api import router, _jobs

    class Jobs:
        def recent_single_results(self):
            return []
        def get(self, job_id):
            raise AssertionError('recent-single must not be treated as a job id')

    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[_jobs] = lambda: Jobs()
    async def request():
        messages = []
        async def receive():
            return {'type': 'http.request', 'body': b'', 'more_body': False}
        async def send(message):
            messages.append(message)
        await app({'type': 'http', 'method': 'GET', 'path': '/api/simulations/recent-single',
                   'query_string': b'', 'headers': [], 'scheme': 'http', 'server': ('test', 80),
                   'client': ('test', 1), 'root_path': '', 'http_version': '1.1'}, receive, send)
        assert next(m['status'] for m in messages if m['type'] == 'http.response.start') == 200
        assert b''.join(m.get('body', b'') for m in messages if m['type'] == 'http.response.body') == b'[]'
    asyncio.run(request())
