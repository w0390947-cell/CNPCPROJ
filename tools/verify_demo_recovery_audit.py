"""Observe Item 11 recovery using the installed package and real AC network.

This audit harness writes evidence only; it does not replace simulator behavior.
"""

import argparse
import json
from datetime import timedelta
from pathlib import Path

import oilfield_energy
from oilfield_energy.bootstrap.adapters.demo_case import DemoNetwork, generate_bundle, load_bundle
from oilfield_energy.modules.demo_simulation.api import DemoService, Fleet
from oilfield_energy.modules.demo_simulation.contracts import Command


def observe(output: Path, old_command: bool, *, rearm: bool = True) -> dict:
    bundle, _ = load_bundle(output / "dataset/bundle.json")
    fleet = Fleet(bundle, DemoNetwork(bundle), "item11-audit")
    journal: list[str] = []
    service = DemoService(fleet, journal.append)
    for _ in range(5):
        service.tick()
    if old_command:
        service.submit(
            Command(
                command_id="before-fault",
                epoch=fleet.epoch,
                device_id="SC-wind-1",
                p_mw=2.0,
                q_mvar=0.0,
                expires_at=service.snapshot().simulated_at + timedelta(minutes=10),
            )
        )
        for _ in range(3):
            service.tick()
    before = service.snapshot()
    service.fault("bad_quality")
    service.fault("wind_trip")
    tripped = service.snapshot()
    # Reset the safety counter after the physical output reduction, so the
    # two normal recovery cycles are explicit and independently observable.
    service.fault("bad_quality")
    service.fault("normal")
    first_safe = service.snapshot()
    early = service.rearm()
    service.tick()
    second_safe = service.snapshot()
    rearmed = service.rearm() if rearm else False
    for _ in range(3):
        service.tick()
    after = service.snapshot()
    name = "old_command" if old_command else "no_command"
    if not rearm:
        name = "no_rearm_control"
    (output / f"{name}.jsonl").write_text("\n".join(journal) + "\n", encoding="utf-8")

    def state(frame):
        device = next(d for d in frame.devices if d.device_id == "SC-wind-1")
        return {
            "minute": frame.sequence - 1,
            "p_mw": device.p_mw,
            "blocked": frame.recovery_blocked,
            "safe_cycles": frame.safe_cycles,
            "all_networks_recovery_safe": all(n.recovery_safe for n in frame.networks),
            "receipts": [r.model_dump(mode="json") for r in frame.receipts],
        }

    return {
        "before": state(before),
        "tripped": state(tripped),
        "first_safe": state(first_safe),
        "second_safe": state(second_safe),
        "early_rearmed": early,
        "rearmed": rearmed,
        "after_three_ticks_without_new_command": state(after),
        "new_commands_after_fault": 0,
        "output_increased_without_new_command": next(
            d.p_mw for d in after.devices if d.device_id == "SC-wind-1"
        )
        > next(d.p_mw for d in second_safe.devices if d.device_id == "SC-wind-1"),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=False)
    generate_bundle(args.output / "dataset")
    result = {
        "package": str(Path(oilfield_energy.__file__).resolve()),
        "no_command": observe(args.output, False),
        "old_command": observe(args.output, True),
        "no_rearm_control": observe(args.output, False, rearm=False),
    }
    (args.output / "summary.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
