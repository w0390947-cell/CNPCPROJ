"""End-to-end numerical and API checks use generated inputs, never client files."""

import hashlib
import json
import socket
import time
from contextlib import contextmanager
from datetime import timedelta
from threading import Thread
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest
import uvicorn

from oilfield_energy.bootstrap.adapters.demo_case import (
    DemoNetwork,
    case_from_bundle,
    generate_bundle,
    load_bundle,
)
from oilfield_energy.bootstrap.demo import create_demo_app
from oilfield_energy.modules.demo_simulation.api import Fleet
from oilfield_energy.modules.demo_simulation.contracts import Command
from oilfield_energy.runtime.journal import RotatingJournal


class Response:
    def __init__(self, status, body):
        self.status_code, self.body = status, body

    def json(self):
        return json.loads(self.body)


class Client:
    def __init__(self, port):
        self.base = f"http://127.0.0.1:{port}"

    def request(self, path, payload=None):
        request = Request(
            self.base + path,
            data=None if payload is None else json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"},
        )
        try:
            with urlopen(request, timeout=15) as response:
                return Response(response.status, response.read())
        except HTTPError as exc:
            return Response(exc.code, exc.read())

    def get(self, path):
        return self.request(path)

    def post(self, path, json):
        return self.request(path, json)


@contextmanager
def running_client(app):
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        server = uvicorn.Server(uvicorn.Config(app, log_level="error"))
        thread = Thread(target=lambda: server.run(sockets=[sock]), daemon=True)
        thread.start()
        try:
            deadline = time.monotonic() + 15
            while not server.started:
                assert thread.is_alive() and time.monotonic() < deadline
                time.sleep(0.02)
            yield Client(sock.getsockname()[1])
        finally:
            server.should_exit = True
            thread.join(15)
            assert not thread.is_alive()


@pytest.fixture
def bundle(tmp_path):
    generate_bundle(tmp_path / "dataset")
    return load_bundle(tmp_path / "dataset/bundle.json")[0]


@pytest.fixture
def fleet(bundle):
    fleet = Fleet(bundle, DemoNetwork(bundle), "epoch-test")
    for _ in range(5):
        fleet.step()
    return fleet


def command(fleet, name="command-1", **kwargs):
    return Command(
        command_id=name,
        epoch=fleet.epoch,
        device_id="SC-storage",
        p_mw=kwargs.pop("p_mw", 0.5),
        q_mvar=kwargs.pop("q_mvar", 0.0),
        expires_at=fleet.frame.simulated_at + timedelta(minutes=10),
        **kwargs,
    )


def test_deterministic_bundle_and_no_silent_checksum_fallback(tmp_path):
    generate_bundle(tmp_path / "a")
    generate_bundle(tmp_path / "b")
    raw = (tmp_path / "a/bundle.json").read_bytes()
    assert raw == (tmp_path / "b/bundle.json").read_bytes()
    (tmp_path / "a/bundle.json").write_bytes(raw + b" ")
    with pytest.raises(ValueError, match="SHA-256"):
        load_bundle(tmp_path / "a/bundle.json")


def test_explicit_profiles_and_independent_wind_devices(bundle):
    case = case_from_bundle(bundle, 8)
    assert len(case.microgrids) == 3
    assert case.assumptions.dt_hours == 3
    assert [
        d.p_max_mw for d in bundle.devices if d.region == "SC" and d.kind == "wind"
    ] == [5, 5]
    assert all(len(a) == 8 for mg in case.microgrids for a in mg.load_p_mw.values())


def test_command_idempotency_ramp_energy_and_replay_conflict(fleet):
    request = command(fleet)
    receipt = fleet.submit(request)
    assert receipt.status == "accepted"
    assert fleet.submit(request) == receipt
    initial = next(r.energy_mwh for r in fleet.readings if r.device_id == "SC-storage")
    first = fleet.step()
    assert first.receipts[-1].status == "executing"
    second = fleet.step()
    assert second.receipts[-1].status == "executed"
    storage = next(r for r in second.devices if r.device_id == "SC-storage")
    assert storage.energy_mwh == pytest.approx(initial - 2 * 0.5 / 0.95 / 60)
    with pytest.raises(ValueError, match="conflicts"):
        fleet.submit(request.model_copy(update={"p_mw": 0.2}))
    assert fleet.submit(request).status == "executed"


@pytest.mark.parametrize(
    "fault",
    ["communication_loss", "bad_quality", "voltage_sag", "nonconvergence", "load_drop"],
)
def test_ac_unknown_or_violation_latches_recovery(fleet, fault):
    fleet.fault = fault
    failed = fleet.step()
    assert failed.recovery_blocked
    assert not all(n.recovery_safe for n in failed.networks)
    if fault in {"communication_loss", "bad_quality", "nonconvergence"}:
        assert all(not n.valid for n in failed.networks)
        assert all(n.voltage_min_pu is None for n in failed.networks)
    assert fleet.submit(command(fleet)).status == "rejected"
    assert not fleet.rearm()
    fleet.fault = "normal"
    assert fleet.step().recovery_blocked
    assert not fleet.rearm()
    fleet.step()
    assert fleet.rearm()
    assert fleet.submit(command(fleet, name="after-rearm")).status == "accepted"


def test_timeout_does_not_imply_not_executed(fleet):
    fleet.fault = "ack_timeout"
    fleet.submit(command(fleet))
    frame = fleet.step()
    assert frame.receipts[-1].status == "timeout"
    assert next(r.p_mw for r in frame.devices if r.device_id == "SC-storage") == 0.5


def test_expiry_supersession_and_capability_rejection(fleet):
    assert (
        fleet.submit(command(fleet, "bad", p_mw=3.0)).reason == "CAPABILITY_VIOLATION"
    )
    fleet.submit(command(fleet, "old"))
    fleet.submit(command(fleet, "new", p_mw=0.25))
    assert fleet.receipts["old"].status == "superseded"
    request = command(fleet, "expiry").model_copy(
        update={"expires_at": fleet.frame.simulated_at + timedelta(seconds=30)}
    )
    fleet.submit(request)
    fleet.step()
    assert fleet.receipts["expiry"].status == "expired"


def test_ac_outputs_have_actual_currents_and_invalid_inputs_not_safe(fleet):
    assert len(fleet.frame.networks) == 3
    for network in fleet.frame.networks:
        assert network.valid and network.loss_mw > 0
        assert all(
            branch["sending_current_a"] is not None
            for branch in network.flow["branches"]
        )


def test_api_lifecycle_fault_commands_and_validation(tmp_path):
    generate_bundle(tmp_path / "bundle")
    app = create_demo_app(
        tmp_path / "bundle/bundle.json", tmp_path / "runs", interval=60
    )
    with running_client(app) as client:
        assert client.get("/api/health").status_code == 200
        frame = client.get("/api/demo/telemetry").json()
        assert client.get("/api/demo/catalog").json()["synthetic"] is True
        availability = client.get("/api/demo/availability")
        assert availability.status_code == 200
        availability_value = availability.json()
        assert (
            availability_value["schema_version"] == "oilfield-resource-availability-v1"
        )
        assert availability_value["source"] == "synthetic_bundle_profile"
        assert availability_value["synthetic"] is True
        assert len(availability_value["series"]) == 7
        assert all(len(item["points"]) == 96 for item in availability_value["series"])
        assert all(
            [point["minute_of_day"] for point in item["points"]]
            == list(range(0, 1440, 15))
            for item in availability_value["series"]
        )
        sc_wind = next(
            item
            for item in availability_value["series"]
            if item["device_id"] == "SC-wind-1"
        )
        assert sc_wind["points"][0]["p_available_mw"] == pytest.approx(2.5)
        payload = dict(
            command_id="api-command",
            epoch=frame["epoch"],
            device_id="SC-storage",
            p_mw=0.25,
            q_mvar=0.0,
            expires_at="2026-01-15T00:10:00Z",
        )
        receipt = client.post("/api/demo/commands", json=payload)
        assert receipt.status_code == 202
        assert receipt.json()["status"] == "accepted"
        assert (
            client.post("/api/demo/commands", json={**payload, "p_mw": 1.0}).status_code
            == 409
        )
        assert (
            client.post("/api/demo/commands", json={**payload, "p_mw": "1"}).status_code
            == 422
        )
        assert client.post("/api/demo/fault", json={"fault": "bad_quality"}).json()[
            "recovery_blocked"
        ]
        assert not client.post("/api/demo/rearm", json={}).json()["rearmed"]
        assert client.get("/api/simulations/not-found").status_code == 404
        assert client.post("/api/simulations", json={"steps": 0}).status_code == 422
    logs = list((tmp_path / "runs").glob("*/telemetry-and-receipts.jsonl"))
    assert logs and len(logs[0].read_text(encoding="utf-8").splitlines()) >= 3


def test_journal_rotates_without_unbounded_memory(tmp_path):
    path = tmp_path / "events.jsonl"
    log = RotatingJournal(path, maximum_bytes=30, backups=2)
    for _ in range(10):
        log.append('{"sample":123}')
    log.close()
    assert len(list(tmp_path.iterdir())) == 3


def test_bad_profiles_fail_after_correctly_rehashed_edit(tmp_path):
    generate_bundle(tmp_path / "data")
    path = tmp_path / "data/bundle.json"
    data = json.loads(path.read_bytes())
    data["case"]["microgrids"][0]["load_p_mw"]["SC_MAIN"][0] = -1
    raw = json.dumps(data).encode()
    path.write_bytes(raw)
    path.with_suffix(".sha256").write_text(
        hashlib.sha256(raw).hexdigest(), encoding="ascii"
    )
    with pytest.raises(ValueError, match="invalid profile"):
        load_bundle(path)


def test_radial_outages_are_reported_as_islanded(bundle):
    screening = DemoNetwork(bundle).screen_contingencies()
    outages = [
        r
        for region in screening.values()
        for r in region["results"]
        if r["scenario"]["contingency_id"]
    ]
    assert len(outages) == 12
    assert all(r["status"] == "islanded" for r in outages)


def test_live_api_independent_worker_result_stream_and_cancel(tmp_path):
    from websockets.sync.client import connect

    generate_bundle(tmp_path / "bundle")
    with running_client(
        create_demo_app(tmp_path / "bundle/bundle.json", tmp_path / "runs", 0.1)
    ) as client:
        started = client.get("/api/demo/telemetry").json()["sequence"]
        job = client.post(
            "/api/simulations", json={"scenario_type": "group_control", "steps": 8}
        ).json()
        job_id = job["simulation_id"]
        with connect(
            client.base.replace("http:", "ws:") + f"/api/simulations/{job_id}/stream"
        ) as socket:
            while True:
                status = json.loads(socket.recv(timeout=30))
                if status["state"] not in {"queued", "running"}:
                    break
        assert status["state"] == "succeeded", status
        assert client.get(f"/api/simulations/{job_id}/result").json()["group_control"][
            "all_passed"
        ]
        assert client.get("/api/demo/telemetry").json()["sequence"] > started
        pending = client.post("/api/simulations", json={"steps": 96}).json()
        queued = client.post("/api/simulations", json={"steps": 96}).json()
        assert (
            client.post(
                f"/api/simulations/{queued['simulation_id']}/cancel", json={}
            ).json()["state"]
            == "cancelled"
        )
        assert (
            client.post(
                f"/api/simulations/{pending['simulation_id']}/cancel", json={}
            ).json()["state"]
            == "cancelled"
        )
    copied = next((tmp_path / "runs").glob(f"*/jobs/{job_id}/bundle.json"))
    assert copied.read_bytes() == (tmp_path / "bundle/bundle.json").read_bytes()
    provenance = json.loads(copied.with_name("input_provenance.json").read_bytes())
    assert provenance["sha256"] == hashlib.sha256(copied.read_bytes()).hexdigest()


def test_runtime_error_is_observable_and_stops_ticks():
    from oilfield_energy.runtime.periodic import PeriodicWorker

    def fail():
        raise ValueError("simulated infrastructure failure")

    worker = PeriodicWorker(fail, 0.01)
    worker.start()
    worker.thread.join(2)
    assert isinstance(worker.error, ValueError)
    assert not worker.thread.is_alive()
    worker.close()
