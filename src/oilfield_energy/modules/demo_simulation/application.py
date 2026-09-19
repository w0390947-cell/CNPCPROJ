"""Repeatable demonstration verification through injected numerical/job ports."""

from collections.abc import Callable
from datetime import timedelta

from pydantic import JsonValue

from .contracts import Bundle, Command, Fault, JobsPort, NetworkPort
from .domain import Fleet


def _verify_recovery_hold(
    bundle: Bundle,
    network: NetworkPort,
    save: Callable[[str, dict[str, JsonValue]], None],
    *,
    old_command: bool,
) -> bool:
    """Exercise actual output after rearm, including per-device authorization."""
    name = "old_command" if old_command else "no_command"
    fleet = Fleet(bundle, network, f"verification-recovery-{name}")
    frame = fleet.step()
    for _ in range(4):
        frame = fleet.step()
    request = Command(
        command_id="before-fault",
        epoch=fleet.epoch,
        device_id="SC-wind-1",
        p_mw=2.0,
        q_mvar=0.0,
        expires_at=frame.simulated_at + timedelta(minutes=30),
    )
    if old_command:
        if fleet.submit(request).status != "accepted":
            return False
        for _ in range(3):
            frame = fleet.step()
        if fleet.receipts[request.command_id].status != "executed":
            return False
    for fault in ("bad_quality", "wind_trip", "bad_quality", "normal"):
        fleet.fault = fault
        frame = fleet.step()
    early = fleet.rearm()
    before = fleet.step()
    rearmed = fleet.rearm()
    save(f"recovery-{name}-before.json", before.model_dump(mode="json"))
    held = not early and rearmed
    for index in range(3):
        frame = fleet.step()
        held = held and all(
            reading.p_mw <= previous.p_mw + 1e-9
            for reading, previous in zip(frame.devices, before.devices, strict=True)
        )
        save(f"recovery-{name}-held-{index}.json", frame.model_dump(mode="json"))
        before = frame
    if old_command:
        # A replay is a query of the earlier receipt, not fresh authorization.
        held = held and fleet.submit(request).status == "executed"
    fresh = request.model_copy(
        update={
            "command_id": "after-rearm",
            "expires_at": frame.simulated_at + timedelta(minutes=10),
        }
    )
    accepted = fleet.submit(fresh)
    for _ in range(3):
        frame = fleet.step()
    save(f"recovery-{name}-fresh-command.json", frame.model_dump(mode="json"))
    return (
        held
        and accepted.status == "accepted"
        and fleet.receipts[fresh.command_id].status == "executed"
        and all(
            abs(r.p_mw - (2.0 if r.device_id == fresh.device_id else 0.0)) < 1e-6
            for r in frame.devices
            if r.kind == "wind"
        )
    )


def verify_demo(
    bundle: Bundle,
    jobs: JobsPort,
    network: NetworkPort,
    *,
    save: Callable[[str, dict[str, JsonValue]], None],
    clock: Callable[[], float],
    wait: Callable[[float], None],
    progress: Callable[[str], None],
) -> dict[str, JsonValue]:
    checks: dict[str, JsonValue] = {}
    screening = network.screen_contingencies()
    save("network-contingencies.json", screening)
    outages: list[JsonValue] = []
    for region in screening.values():
        if not isinstance(region, dict):
            continue
        rows = region.get("results")
        if not isinstance(rows, list):
            continue
        for result in rows:
            if not isinstance(result, dict):
                continue
            scenario = result.get("scenario")
            if isinstance(scenario, dict) and scenario.get("contingency_id") is not None:
                outages.append(result.get("status"))
    checks["radial_n_minus_one_reports_islanding"] = len(outages) == 12 and all(
        v == "islanded" for v in outages
    )
    for name, request in bundle.presets.items():
        progress(f"Running {name}")
        status = jobs.create(request)
        job_id = str(status["simulation_id"])
        deadline = clock() + 1800
        while status["state"] in {"queued", "running"}:
            if clock() >= deadline:
                jobs.cancel(job_id)
                raise RuntimeError(f"verification timed out: {name}")
            wait(0.25)
            status = jobs.get(job_id)
        save(f"{name}-status.json", status)
        if status["state"] != "succeeded":
            raise RuntimeError(f"{name} failed: {status.get('error_message')}")
        result = jobs.result(job_id)
        save(f"{name}-result.json", result)
        summary = result.get("executive_summary")
        checks[name] = bool(isinstance(summary, dict) and summary.get("overall_passed"))
        progress(f"Finished {name}: engineering checks={checks[name]}")
    faults: tuple[Fault, ...] = (
        "normal",
        "communication_loss",
        "bad_quality",
        "voltage_sag",
        "ack_timeout",
        "load_drop",
        "wind_trip",
        "nonconvergence",
    )
    for fault in faults:
        fleet = Fleet(bundle, network, f"verification-{fault}")
        for _ in range(5):
            fleet.step()
        fleet.fault = fault
        ack_accepted = None
        if fault == "ack_timeout":
            assert fleet.frame is not None
            ack_accepted = fleet.submit(
                Command(
                    command_id="verify-ack-timeout",
                    epoch=fleet.epoch,
                    device_id="SC-storage",
                    p_mw=0.5,
                    q_mvar=0.0,
                    expires_at=fleet.frame.simulated_at + timedelta(minutes=10),
                )
            )
            save("ack-timeout-accepted.json", ack_accepted.model_dump(mode="json"))
        failed = fleet.step()
        save(f"telemetry-{fault}.json", failed.model_dump(mode="json"))
        expected_block = fault in {
            "communication_loss",
            "bad_quality",
            "voltage_sag",
            "load_drop",
            "nonconvergence",
        }
        checks[f"fault:{fault}"] = failed.recovery_blocked == expected_block
        if ack_accepted is not None:
            receipt = fleet.receipts[ack_accepted.command.command_id]
            actual = next(r for r in failed.devices if r.device_id == "SC-storage")
            checks["fault:ack_timeout"] = (
                not failed.recovery_blocked
                and ack_accepted.status == "accepted"
                and receipt.status == "timeout"
                and receipt.reason == "ACK_NOT_RECEIVED_EXECUTION_UNKNOWN"
                and abs(actual.p_mw - 0.5) < 1e-6
            )
        if expected_block:
            fleet.fault = "normal"
            fleet.step()
            early = fleet.rearm()
            fleet.step()
            checks[f"rearm:{fault}"] = not early and fleet.rearm()
    for old_command in (False, True):
        name = "old_command" if old_command else "no_command"
        checks[f"recovery_hold:{name}"] = _verify_recovery_hold(
            bundle, network, save, old_command=old_command
        )
    fleet = Fleet(bundle, network, "verification-command")
    frame = fleet.step()
    for _ in range(4):
        frame = fleet.step()
    request = Command(
        command_id="verify-command",
        epoch=fleet.epoch,
        device_id="SC-storage",
        p_mw=0.5,
        q_mvar=0.0,
        expires_at=frame.simulated_at + timedelta(minutes=10),
    )
    accepted = fleet.submit(request)
    executing = fleet.step()
    executed = fleet.step()
    checks["command_lifecycle"] = (
        accepted.status == "accepted"
        and executing.receipts[-1].status == "executing"
        and executed.receipts[-1].status == "executed"
    )
    save("telemetry-command-executed.json", executed.model_dump(mode="json"))
    report: dict[str, JsonValue] = {
        "dataset_id": bundle.dataset_id,
        "synthetic": True,
        "checks": checks,
        "passed": all(v is True for v in checks.values()),
    }
    save("verification.json", report)
    return report
