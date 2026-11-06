"""The disclosure studies share the actual packaged network and sliced clock."""

from dataclasses import replace

import numpy as np
import pytest

from oilfield_energy.bootstrap.adapters.cluster_study import (
    ClusterStudySpec,
    build_cluster_study,
)
from oilfield_energy.bootstrap.adapters.project_dataset import build_synthetic_case
from oilfield_energy.bootstrap.adapters.cluster_execution import execution_inputs
from oilfield_energy.network_model import validate_legacy_network_alignment
from oilfield_energy.bootstrap.adapters.demo_case import build_bundle, case_from_bundle


def test_contiguous_slice_preserves_network_ratings_permissions_and_native_tariff():
    base = build_synthetic_case(96)
    case = build_cluster_study(ClusterStudySpec(source_start_minute=405))
    assert [len(m.buses) for m in case.microgrids] == [10, 7, 7]
    assert case.cluster_import_limit_mw == 26
    np.testing.assert_array_equal(case.time_hours, [0, 0.25, 0.5, 0.75])
    np.testing.assert_array_equal(case.price_cny_per_mwh, base.price_cny_per_mwh[27:31])
    assert case.price_cny_per_mwh.tolist() == [280, 720, 720, 720]
    for current, original in zip(case.microgrids, base.microgrids, strict=True):
        assert current.network_model_v2 == original.network_model_v2
        assert current.lines == original.lines
        assert current.storage == original.storage
        assert current.pv_reactive_enabled == original.pv_reactive_enabled
        assert current.storage_reactive_enabled == original.storage_reactive_enabled
        for key in ("load_p_mw", "load_q_mvar", "wind_available_mw", "pv_available_mw"):
            for bus, values in getattr(current, key).items():
                np.testing.assert_array_equal(
                    values, getattr(original, key)[bus][27:31]
                )
    _, plants, _ = execution_inputs(case, packaged=False, events=[])
    assert all(p.steps == 60 and p.step_minutes == 1 for p in plants.values())
    for grid in case.microgrids:
        for row in plants[grid.name].load_p:
            np.testing.assert_array_equal(
                row.values, grid.load_p_mw[row.bus_id].repeat(15)
            )


def test_low_energy_uses_each_original_lower_bound_and_no_implicit_network_changes():
    normal = build_cluster_study()
    low = build_cluster_study(ClusterStudySpec(low_energy=True))
    for a, b in zip(normal.microgrids, low.microgrids, strict=True):
        assert b.storage.e_initial_mwh == a.storage.e_min_mwh
        assert replace(b.storage, e_initial_mwh=a.storage.e_initial_mwh) == a.storage
        assert a.network_model_v2 == b.network_model_v2
        assert a.initial_resource_q_mvar == b.initial_resource_q_mvar
    assert [m.storage.e_initial_mwh for m in low.microgrids] == pytest.approx(
        [0.5, 0.4, 0.36]
    )


def test_scenario_rating_changes_both_authority_and_legacy_view_without_mutating_base():
    base = build_cluster_study()
    line = base.microgrids[0].lines[-1]
    changed = build_cluster_study(
        ClusterStudySpec(
            branch_ratings_mva={"SC": {line.name: 0.8}}, cluster_import_limit_mw=25
        )
    )
    resolved = validate_legacy_network_alignment(changed.microgrids[0])
    assert (
        next(b.s_max_mva for b in resolved.branches if b.branch_id == line.name) == 0.8
    )
    assert build_cluster_study().microgrids[0].lines[-1] == line
    assert changed.cluster_import_limit_mw == 25


@pytest.mark.parametrize(
    "kwargs",
    [
        {"source_start_minute": 1},
        {"source_start_minute": 1410, "duration_minutes": 60},
        {"duration_minutes": 0},
        {"initial_energy_mwh": {"SC": -1}},
        {"branch_ratings_mva": {"SC": {"unknown": 1}}},
        {"initial_energy_mwh": {"unknown": 1}},
    ],
)
def test_invalid_window_or_override_fails_explicitly(kwargs):
    with pytest.raises(ValueError):
        build_cluster_study(ClusterStudySpec(**kwargs))


def test_initial_state_must_be_complete_and_is_not_a_permission_to_exceed_ratings():
    grid = build_cluster_study().microgrids[0]
    with pytest.raises(ValueError, match="initial P/Q"):
        replace(grid, initial_resource_p_mw={"unknown": 1})


def test_old_empty_planning_maps_decode_but_computed_envelopes_do_not():
    bundle = build_bundle()
    raw = dict(bundle.case)
    raw.update(storage_reserves={}, reactive_plans={})
    assert (
        len(case_from_bundle(bundle.model_copy(update={"case": raw}), 8).microgrids)
        == 3
    )
    raw["storage_reserves"] = {"SC": {"untrusted": True}}
    with pytest.raises(ValueError, match="unexpected case fields"):
        case_from_bundle(bundle.model_copy(update={"case": raw}), 8)
