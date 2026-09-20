"""Real solver regressions for item 10's identity, capacity and archive defects."""

import json
from dataclasses import replace
from oilfield_energy.bootstrap.adapters.project_dataset import study_recipe
from pathlib import Path

import pytest

from oilfield_energy.bootstrap.shancheng_simulation import (
    create_simulation,
    generate_simulation,
)
from oilfield_energy.minute_network import MinuteNetworkEvaluator
from oilfield_energy.modules.dispatch.api import read_adopted_schedule
from oilfield_energy.modules.dispatch.contracts import AdoptedSchedule
from oilfield_energy.power_flow_comparison import read_comparison_request

ROOT = Path(__file__).resolve().parents[2]


def read(path):
    return json.loads(path.read_text(encoding="utf-8"))


@pytest.fixture(scope="module", params=["renamed", "s_binding", "q_binding"])
def study(request, tmp_path_factory):
    folder = tmp_path_factory.mktemp(request.param)
    recipe = json.loads(study_recipe().model_dump_json())
    recipe.update(intervals=8, start="2026-09-15T12:00:00+08:00", rolling_horizon_intervals=4)
    for index, resource in enumerate(recipe["resources"]):
        if request.param == "renamed":
            resource["resource_id"] = f"study-device-{index + 1}"
        if resource["kind"] == "svg":
            if request.param == "s_binding":
                resource["s_max_mva"] = 1.5
            if request.param == "q_binding":
                resource["q_max_mvar"] = 1.5
    config = folder / "recipe.json"
    config.write_text(json.dumps(recipe), encoding="utf-8")
    generate_simulation(config, folder / "inputs")
    outcome = create_simulation().execute(folder / "inputs/manifest.json", folder / "results")
    assert outcome.exit_code == 0, read(folder / "results/study_summary.json")
    return recipe, folder / "results"


def test_ids_and_first_step_commands_survive_all_stages(study):
    recipe, output = study
    ids = {r["resource_id"] for r in recipe["resources"]}
    plan = read_adopted_schedule(
        (output / "executed_interval_plan.json").read_text(encoding="utf-8")
    )
    assert isinstance(plan, AdoptedSchedule)
    assert {r.resource_id for r in plan.resource_schedules} == ids
    assert len(plan.origins) == 8 and plan.objective_cny is None
    ahead = read(output / "day_ahead.json")["optimization"]
    assert {r["resource_id"] for r in ahead["microgrids"]["SC"]["resource_schedules"]} == ids
    for index in range(8):
        row = read(output / "intraday" / f"{index:03d}.json")
        schedules = row["result"]["optimization"]["microgrids"]["SC"]["resource_schedules"]
        by_id = {s["resource_id"]: s for s in schedules}
        assert set(by_id) == ids
        assert plan.origins[index].window_end_interval == min(8, index + 4)
        for resource in plan.resource_schedules:
            assert (
                resource.active_power_mw[index] == by_id[resource.resource_id]["active_power_mw"][0]
            )
            assert (
                resource.reactive_power_mvar[index]
                == by_id[resource.resource_id]["reactive_power_mvar"][0]
            )
    with (output / "minute_normal/minute_network.jsonl").open(encoding="utf-8") as file:
        for line in file:
            record = json.loads(line)
            assert {d["resource_id"] for d in record["inputs"]["snapshot"]["devices"]} == ids


def test_svg_plan_execution_and_safety_evidence_obey_both_limits(study):
    recipe, output = study
    declared = next(r for r in recipe["resources"] if r["kind"] == "svg")
    bound = min(declared["s_max_mva"], declared["q_max_mvar"])
    plan = read(output / "executed_interval_plan.json")
    svg = next(r for r in plan["resource_schedules"] if r["resource_type"] == "svg")
    assert max(map(abs, svg["reactive_power_mvar"])) <= bound + 1e-7
    tracking = read(output / "minute_normal/tracking.json")
    assert max(map(abs, tracking["svg_reactive_actual_mvar"])) <= bound + 1e-7
    with (output / "minute_normal/minute_network.jsonl").open(encoding="utf-8") as file:
        for line in file:
            record = json.loads(line)
            device = next(
                d for d in record["inputs"]["snapshot"]["devices"] if d["resource_type"] == "svg"
            )
            assert device["s_max_mva"] == declared["s_max_mva"]
            assert device["q_max_mvar"] == declared["q_max_mvar"]
            assert record["device_capacity_within_limits"] is True


def test_device_overcapacity_blocks_recovery_even_when_ac_converges():
    request = read_comparison_request(ROOT / "tests/fixtures/field_dataset/fixed_device_comparison.json")
    devices = tuple(
        replace(d, s_max_mva=0.1) if d.resource_type.value == "svg" else d
        for d in request.snapshot.devices
    )
    assert any(d.resource_type.value == "svg" for d in devices)
    snapshot = replace(request.snapshot, devices=devices)
    evaluator = MinuteNetworkEvaluator(
        request.network, request.limits, {d.resource_id: d.bus_id for d in devices}
    )
    feedback = evaluator.evaluate(snapshot, at=snapshot.at, operating_mode_id="normal")
    assert feedback.valid
    assert feedback.device_capacity_within_limits is False
    assert not feedback.recovery_safe
    assert any(v["code"] == "S_MAX" for v in feedback.device_violations)
