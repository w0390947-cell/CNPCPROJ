"""Regional policy selection survives all projections and archive round trips."""

import json
from dataclasses import replace

import pytest

from oilfield_energy.bootstrap.adapters.demo_case import (
    build_bundle,
    case_from_bundle,
    decode_bundle,
    encode,
)
from oilfield_energy.bootstrap.adapters.project_dataset import build_synthetic_case, load_dataset
from oilfield_energy.bootstrap.adapters.simulation_case import make_case
from oilfield_energy.modules.studies.api import generate_inputs
from oilfield_energy.modules.studies.contracts import StudySpec


def test_default_regions_and_demo_use_explicit_source_policies():
    case = build_synthetic_case(4)
    bundle = decode_bundle(build_bundle().model_dump_json().encode())
    restored = case_from_bundle(bundle, 4)
    for original, captured in zip(case.microgrids, restored.microgrids, strict=True):
        expected = 0.30 if original.name == "SC" else 0.333
        assert set(original.wind_q_abs_over_p_max.values()) == {expected}
        assert original.wind_reactive_policy == captured.wind_reactive_policy
        assert original.wind_q_abs_max_mvar == captured.wind_q_abs_max_mvar
        assert original.wind_capacity_provenance == captured.wind_capacity_provenance
        for device in bundle.devices:
            if device.region == original.name and device.kind == "wind":
                assert device.q_abs_over_p_max == expected
                assert device.q_max_mvar == original.wind_q_abs_max_mvar[device.bus_id]


def test_v2_requires_policy_and_capacity_provenance():
    source = json.loads(load_dataset()[0].regions[0].model_dump_json())
    del source["wind_reactive_policy"]
    with pytest.raises(ValueError, match="explicit wind reactive policy"):
        StudySpec.model_validate_json(json.dumps(source))
    source = json.loads(load_dataset()[0].regions[0].model_dump_json())
    source["resources"][0].pop("capacity_provenance")
    with pytest.raises(ValueError, match="capacity provenance"):
        StudySpec.model_validate_json(json.dumps(source))


def test_policy_is_not_inferred_from_region_name():
    spec = load_dataset()[0].regions[1]
    renamed = spec.model_copy(update={"region_id": "SC"})
    network = build_synthetic_case(4).microgrids[1].network_model_v2
    case = make_case(renamed, network, generate_inputs(renamed)[0])
    assert set(case.microgrids[0].wind_q_abs_over_p_max.values()) == {0.333}


def test_policy_cannot_be_widened_by_a_downstream_parameter_override():
    sc = build_synthetic_case(4).microgrids[0]
    with pytest.raises(ValueError, match="exceeds the selected policy"):
        replace(sc, wind_q_abs_over_p_max={bus: 0.333 for bus in sc.wind_capacity_mw})
    payload = json.loads(build_bundle().model_dump_json())
    payload["devices"][0]["q_abs_over_p_max"] = 0.333
    with pytest.raises(ValueError, match="wind device capability"):
        decode_bundle(encode(payload))


def test_historical_v1_recipe_retains_its_original_ratio_and_no_new_rating_constraint():
    current = load_dataset()[0].regions[0]
    payload = json.loads(current.model_dump_json())
    payload["schema_version"] = "shancheng-simulation-v1"
    payload["revision"] = "historical-v1"
    payload.pop("wind_reactive_policy")
    for resource in payload["resources"]:
        resource.pop("capacity_provenance", None)
    old = StudySpec.model_validate_json(json.dumps(payload))
    network = build_synthetic_case(4).microgrids[0].network_model_v2
    case = make_case(old, network, generate_inputs(old)[0])
    mg = case.microgrids[0]
    assert mg.wind_reactive_policy is None
    assert mg.wind_q_abs_max_mvar is None
    assert all(ratio == pytest.approx(0.328) for ratio in mg.wind_q_abs_over_p_max.values())


def test_old_captured_bundle_does_not_acquire_new_policy_when_decoded():
    payload = json.loads(build_bundle().model_dump_json())
    for mg in payload["case"]["microgrids"]:
        for key in ("wind_reactive_policy", "wind_q_abs_max_mvar", "wind_capacity_provenance"):
            mg.pop(key)
        mg["wind_q_abs_over_p_max"] = {bus: 0.328 for bus in mg["wind_capacity_mw"]}
    for device in payload["devices"]:
        if device["kind"] == "wind":
            device["q_abs_over_p_max"] = 0.328
    payload["case"]["dataset_revision"] = "historical-v1"
    bundle = decode_bundle(encode(payload))
    case = case_from_bundle(bundle, 4)
    assert case.dataset_revision == "historical-v1"
    assert all(m.wind_reactive_policy is None for m in case.microgrids)
    assert all(set(m.wind_q_abs_over_p_max.values()) == {0.328} for m in case.microgrids)
