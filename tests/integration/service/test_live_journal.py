import asyncio
import json
import subprocess
import sys
import time
from oilfield_energy.live_journal import LiveJournal, read_live


def test_live_selects_current_attempt_instead_of_stale_records(tmp_path):
    import pytest
    from oilfield_energy.job_manager import SimulationJobManager, JobStatus, atomic_write_json, write_job_status, utc_now
    manager = SimulationJobManager(tmp_path / 'jobs')
    try:
        job_id = 'a' * 32
        session = manager.root / job_id
        session.mkdir()
        now = utc_now()
        write_job_status(session, JobStatus(simulation_id=job_id, name='fixture', state='cancelled', created_at=now, updated_at=now))
        legacy = LiveJournal(session)
        legacy.publish({'kind': 'initial', 'source': 'legacy'})
        assert manager.live(job_id)['events'][0]['source'] == 'legacy'
        previous = legacy.stream
        for attempt_id in ['b' * 32, 'c' * 32]:
            attempt = session / 'attempts' / attempt_id
            attempt.mkdir(parents=True)
            journal = LiveJournal(attempt)
            journal.publish({'kind': 'initial', 'source': attempt_id})
            atomic_write_json(session / 'execution.json', {'attempt_id': attempt_id})
            batch = manager.live(job_id, 99, previous)
            assert batch['cursor'] == 1 and batch['events'][0]['source'] == attempt_id
            previous = batch['stream']
        atomic_write_json(session / 'execution.json', {'attempt_id': '../outside'})
        with pytest.raises(KeyError):
            manager.live(job_id)
    finally:
        manager.close()


def test_live_http_reads_real_worker_attempt_and_survives_cancel(tmp_path, monkeypatch):
    from tests.integration.service.test_job_lifecycle import server
    from oilfield_energy.service import SimulationRequest

    # Keep the real queue, isolated process, execution manifest and HTTP reader.
    # Only replace numerical work with a small event producer.
    original_popen = subprocess.Popen
    producer = """
import sys
from pathlib import Path
from oilfield_energy.live_journal import LiveJournal
session = Path(sys.argv[sys.argv.index('--session') + 1])
journal = LiveJournal(session)
journal.publish({'kind': 'initial'})
for minute in range(1, 65):
    journal.publish({'kind': 'cycle', 'minute': minute})
sys.stdin.read()
"""
    def start(command, **kwargs):
        return original_popen([sys.executable, '-c', producer, *command[3:]], **kwargs)
    monkeypatch.setattr(subprocess, 'Popen', start)
    with server(tmp_path / 'jobs') as (http, _app):
        job = http('/api/simulations', SimulationRequest().model_dump(mode='json'))
        job_id = job['simulation_id']
        session = tmp_path / 'jobs' / job_id
        deadline = time.monotonic() + 15
        while True:
            execution = json.loads((session / 'execution.json').read_bytes())
            attempt = session / 'attempts' / execution['attempt_id']
            manifests = list((attempt / 'live').glob('*/00000065.json'))
            if manifests:
                break
            assert time.monotonic() < deadline
            time.sleep(.02)
        assert not (session / 'live.json').exists()
        first = http(f'/api/simulations/{job_id}/live')
        assert len(first['events']) == 32 and first['events'][0]['kind'] == 'initial'
        second = http(f'/api/simulations/{job_id}/live?after=32&stream={first["stream"]}')
        assert second['events'][0]['sequence'] == 33
        assert http(f'/api/simulations/{job_id}/live?after=32&stream={first["stream"]}') == second
        http(f'/api/simulations/{job_id}/cancel', {})
        final = http(f'/api/simulations/{job_id}/live?after=64&stream={first["stream"]}')
        assert final['events'][0]['minute'] == 64 and not final['more']


def test_live_journal_pages_reconnect_and_new_attempt(tmp_path):
    assert read_live(tmp_path)["events"] == []
    journal = LiveJournal(tmp_path)
    for minute in range(70):
        journal.publish({"kind": "cycle", "minute": minute})
    first = read_live(tmp_path)
    assert len(first["events"]) == 32 and first["more"]
    second = read_live(tmp_path, first["cursor"], first["stream"])
    assert second["events"][0]["sequence"] == 33
    assert read_live(tmp_path, first["cursor"], first["stream"]) == second
    final = read_live(tmp_path, second["cursor"], second["stream"])
    assert final["cursor"] == 70 and not final["more"]
    replacement = LiveJournal(tmp_path)
    replacement.publish({"kind": "initial"})
    reset = read_live(tmp_path, 70, journal.stream)
    assert reset["cursor"] == 1 and reset["stream"] != journal.stream


def test_live_route_validates_cursor_and_unknown_job():
    from fastapi import FastAPI
    from oilfield_energy.web_api import router, _jobs
    class Jobs:
        def live(self, job, after, stream):
            if job == "missing":
                raise KeyError(job)
            return {"stream": None, "events": [], "cursor": 0, "more": False}
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[_jobs] = lambda: Jobs()
    async def get(path, query=b''):
        messages = []
        async def receive():
            return {"type": "http.request", "body": b'', "more_body": False}
        async def send(message):
            messages.append(message)
        await app({"type": "http", "method": "GET", "path": path, "query_string": query,
                   "headers": [], "scheme": "http", "server": ("test", 80),
                   "client": ("test", 1), "root_path": "", "http_version": "1.1"}, receive, send)
        return next(m['status'] for m in messages if m['type'] == 'http.response.start')
    assert asyncio.run(get('/api/simulations/job/live')) == 200
    assert asyncio.run(get('/api/simulations/job/live', b'after=-1')) == 422
    assert asyncio.run(get('/api/simulations/missing/live')) == 404
