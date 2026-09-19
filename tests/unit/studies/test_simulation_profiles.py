import json
from datetime import timedelta
from pathlib import Path

import pytest

from oilfield_energy.modules.control.contracts import PlantInputs
from oilfield_energy.modules.studies.api import generate_inputs
from oilfield_energy.modules.studies.contracts import ScenarioCatalog, StudySpec

ROOT = Path(__file__).resolve().parents[3]


def recipe():
    return json.loads(
        (ROOT / "examples/shancheng_simulation/recipe.json").read_text(encoding="utf-8")
    )


@pytest.mark.parametrize("seed", [1, 20260915, 987654])
def test_seeded_complete_profiles_and_physical_bounds(seed):
    raw = recipe()
    raw["seed"] = seed
    spec = StudySpec.model_validate_json(json.dumps(raw), strict=True)
    before = spec.model_dump_json()
    first = generate_inputs(spec)
    assert first == generate_inputs(spec)
    assert spec.model_dump_json() == before
    assert [p.steps for p in first] == [96, 96, 1440]
    assert first[0] != first[1]
    for profile in first:
        assert {x.bus_id for x in profile.load_p} == set(spec.bus_ids)
        for s in (*profile.wind_available, *profile.pv_available):
            bound = next(r.p_max_mw for r in spec.resources if r.bus_id == s.bus_id)
            assert all(0 <= x <= bound for x in s.values)
        for s in profile.pv_available:
            assert s.values[0] == 0


@pytest.mark.parametrize(
    "change",
    [
        lambda r: r.update(synthetic=False),
        lambda r: r.update(interval_minutes=60),
        lambda r: r.update(start="2026-09-15T00:00:00"),
        lambda r: r["resources"][0].update(bus_id="absent"),
        lambda r: r["storage"].update(initial_mwh=20.0),
        lambda r: r.update(unknown=1),
    ],
)
def test_recipe_rejects_invalid_or_non_synthetic_input(change):
    raw = recipe()
    change(raw)
    with pytest.raises(ValueError):
        StudySpec.model_validate_json(json.dumps(raw), strict=True)


def test_plant_window_and_missing_nonfinite_values():
    spec = StudySpec.model_validate_json(json.dumps(recipe()), strict=True)
    plant = generate_inputs(spec)[2]
    cut = plant.window(30, 60, plant.start + timedelta(minutes=30))
    assert cut.steps == 30 and cut.load_p[0].values == plant.load_p[0].values[30:60]
    raw = json.loads(plant.model_dump_json())
    raw["load_p"][0]["values"][0] = float("nan")
    with pytest.raises(ValueError):
        PlantInputs.model_validate_json(json.dumps(raw))
    raw["load_p"][0]["values"] = []
    with pytest.raises(ValueError):
        PlantInputs.model_validate_json(json.dumps(raw))


def test_scenario_contract_is_strict_and_generated_schemas_match():
    for name, model in (
        ("recipe", StudySpec),
        ("plant", PlantInputs),
        ("scenarios", ScenarioCatalog),
    ):
        schema = json.loads(
            (ROOT / f"contracts/simulation/{name}.schema.json").read_text(encoding="utf-8")
        )
        assert schema == model.model_json_schema()
    with pytest.raises(ValueError):
        ScenarioCatalog.model_validate_json(
            json.dumps(
                dict(
                    schema_version="SC-scenarios-v1",
                    seed=1,
                    cases=["normal"] * 7,
                    fault_start_minute=10,
                    fault_end_minute=15,
                    rearm_minute=20,
                    window_intervals=4,
                )
            )
        )
