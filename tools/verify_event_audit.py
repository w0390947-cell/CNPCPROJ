"""Observe actual scenario-event execution without replacing solver or channel behavior."""

from __future__ import annotations

import argparse
import hashlib
import json
from collections import Counter
from dataclasses import asdict
from pathlib import Path
from typing import Any
from unittest.mock import patch

import numpy as np
from pydantic import ValidationError

import oilfield_energy
from oilfield_energy import service
from oilfield_energy.communication import SimulatedCommunicationChannel
from oilfield_energy.data import build_synthetic_case
from oilfield_energy.scenario_events import ScenarioEvent, apply_physical_events
from oilfield_energy.service import SimulationRequest, run_simulation


def write(path: Path, value: Any) -> None:
    path.write_text(
        json.dumps(value, indent=2, ensure_ascii=True, allow_nan=False), encoding="utf-8"
    )


def event(
    identity: str, kind: str, target: str, start: float, end: float, magnitude: float = 1.0
) -> ScenarioEvent:
    return ScenarioEvent.model_validate(
        {
            "event_id": identity,
            "event_type": kind,
            "target": target,
            "time_axis": "coordination_iteration"
            if kind.startswith("communication")
            else "clock_minute",
            "start": start,
            "end": end,
            "magnitude": magnitude,
            "label": identity,
        }
    )


def observe(request: SimulationRequest, output: Path) -> dict[str, Any]:
    sent: list[dict[str, Any]] = []
    configs: list[dict[str, Any]] = []
    original_send = SimulatedCommunicationChannel.send
    original_admm = service.run_admm_coordination

    def observe_send(
        channel: SimulatedCommunicationChannel,
        sender: str,
        receiver: str,
        payload: Any,
        iteration: int,
    ) -> bool:
        accepted = original_send(channel, sender, receiver, payload, iteration)
        sent.append(
            {"sender": sender, "receiver": receiver, "tick": iteration, "accepted": accepted}
        )
        return accepted

    def observe_admm(*args: Any, **kwargs: Any) -> Any:
        configs.append(asdict(kwargs["communication_config"]))
        return original_admm(*args, **kwargs)

    with (
        patch.object(SimulatedCommunicationChannel, "send", observe_send),
        patch.object(service, "run_admm_coordination", observe_admm),
    ):
        result = run_simulation(request)
    payload = result.model_dump(mode="json")
    write(output / f"{request.name}_result.json", payload)
    write(output / f"{request.name}_messages.json", sent)
    drops = [message for message in sent if not message["accepted"]]
    summary = {
        "overall_passed": result.executive_summary.overall_passed,
        "request_events": [item.model_dump(mode="json") for item in request.events],
        "actual_configs": configs,
        "dropped_by_region": dict(
            Counter(m["receiver"] if m["sender"] == "coordinator" else m["sender"] for m in drops)
        ),
        "drop_ticks": sorted({m["tick"] for m in drops}),
        "communication": payload["communication"],
        "history_count": len(payload["admm_history"]),
        "group_digest": hashlib.sha256(
            json.dumps(payload["group_control"], sort_keys=True).encode()
        ).hexdigest(),
        "trajectory_digest": hashlib.sha256(
            json.dumps(
                {key: payload[key] for key in ("timeseries", "cluster_timeseries", "admm_history")},
                sort_keys=True,
            ).encode()
        ).hexdigest(),
    }
    print(request.name, summary["overall_passed"], summary["drop_ticks"], flush=True)
    return summary


def collect(output: Path, expect_repaired: bool = False) -> dict[str, Any]:
    outage_a = event("outage_SC_early", "communication_outage", "SC", 3, 8)
    outage_b = event("outage_YAB_later", "communication_outage", "YA_B", 12, 16)
    loss_b = event("loss_YAB", "communication_packet_loss", "YA_B", 3, 8)
    loss_c = event("loss_YAC", "communication_packet_loss", "YA_C", 3, 8)
    requests = {
        "outage_single": ("communication_fault", [outage_a]),
        "outage_multiple": ("communication_fault", [outage_a, outage_b]),
        "outage_reversed": ("communication_fault", [outage_b, outage_a]),
        "loss_yab": ("communication_fault", [loss_b]),
        "loss_yac": ("communication_fault", [loss_c]),
        "cluster_normal": ("cluster_coordination", []),
        "cluster_with_outage": ("cluster_coordination", [outage_a]),
        "group_normal": ("group_control", []),
        "group_with_load_drop": (
            "group_control",
            [event("load_halved", "load_drop", "SC", 0, 1440, 0.5)],
        ),
        "single_normal": ("single_microgrid", []),
        "single_unsampled": (
            "single_microgrid",
            [event("load_short", "load_drop", "SC", 1, 2, 0.5)],
        ),
    }
    runs = {}
    for name, (kind, events) in requests.items():
        payload = {
            "name": name,
            "scenario_type": kind,
            "steps": 4,
            "events": events,
            "communication_loss_probability": 0,
            "communication_max_delay_iterations": 0,
            "communication_outage_start_iteration": 1000,
            "communication_outage_end_iteration": 1000,
        }
        try:
            request = SimulationRequest.model_validate(payload)
        except ValidationError as exc:
            if not expect_repaired or name not in {"cluster_with_outage", "group_with_load_drop"}:
                raise
            runs[name] = {"request_rejected": True, "reason": str(exc)}
            write(output / f"{name}_rejection.json", runs[name])
            continue
        runs[name] = observe(request, output)

    if expect_repaired:
        assert runs["outage_multiple"]["dropped_by_region"] == {"SC": 12, "YA_B": 10}
        assert (
            runs["outage_multiple"]["trajectory_digest"]
            == runs["outage_reversed"]["trajectory_digest"]
        )
        assert runs["cluster_with_outage"]["request_rejected"]
        assert runs["group_with_load_drop"]["request_rejected"]

    source = build_synthetic_case(steps=96)
    initial = {
        mg.name: {key: array.copy() for key, array in mg.load_p_mw.items()}
        for mg in source.microgrids
    }
    physical = {}
    for name, start, end in (("aligned", 0, 15), ("between_samples", 1, 2)):
        changed = apply_physical_events(source, [event(name, "load_drop", "SC", start, end, 0.5)])
        counts = {}
        for before, after in zip(source.microgrids, changed.microgrids):
            counts[before.name] = sum(
                int(np.count_nonzero(before.load_p_mw[key] != array))
                for key, array in after.load_p_mw.items()
            )
        physical[name] = counts
    physical["source_unchanged"] = all(
        np.array_equal(initial[mg.name][key], array)
        for mg in source.microgrids
        for key, array in mg.load_p_mw.items()
    )
    return {"runs": runs, "physical_checks": physical}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--expect-repaired", action="store_true")
    parser.add_argument(
        "--baseline-manifest",
        type=Path,
        default=Path("artifacts/audit_20260918/item09/source_manifest_before_audit.json"),
    )
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=False)
    result = collect(args.output, args.expect_repaired)
    write(args.output / "evidence.json", result)
    root = Path(__file__).resolve().parents[1]
    manifest = {
        str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in sorted((root / "src/oilfield_energy").rglob("*.py"))
    }
    before = json.loads((root / args.baseline_manifest).read_text(encoding="utf-8"))
    package = Path(oilfield_energy.__file__).resolve().parent
    matches = all(
        hashlib.sha256(
            (package / Path(path).relative_to("src/oilfield_energy")).read_bytes()
        ).hexdigest()
        == digest
        for path, digest in manifest.items()
    )
    write(
        args.output / "integrity.json",
        {
            "source_unchanged": manifest == before,
            "source_count": len(manifest),
            "package_root": str(package),
            "package_matches_source": matches,
        },
    )
    assert manifest == before and matches


if __name__ == "__main__":
    main()
