"""Both legacy solver backends honor the same identity/capability projection."""

from dataclasses import replace

import pytest

from oilfield_energy.ac_consistency import solve_case_ac_consistent
from tests.legacy_case_fixture import build_synthetic_case
from oilfield_energy.model import solve_case
from oilfield_energy.modules.resources.contracts import ResourceIdentity


@pytest.mark.parametrize("backend", ["milp", "misocp"])
@pytest.mark.parametrize("s,q", [(1.5, 2.5), (2.5, 1.5)])
def test_solver_preserves_declared_ids_and_svg_intersection(backend, s, q):
    case = build_synthetic_case(steps=4)
    mg = case.microgrids[0]
    keys = (
        [("wind", b) for b in mg.wind_available_mw]
        + [("pv", b) for b in mg.pv_available_mw]
        + [("storage", mg.storage.bus), ("svg", mg.svg_bus)]
    )
    ids = tuple(ResourceIdentity(f"declared-{i}", bus, kind) for i, (kind, bus) in enumerate(keys))
    mg = replace(mg, resource_identities=ids, svg_s_max_mva=s, svg_q_min_mvar=-q, svg_q_max_mvar=q)
    case = replace(case, microgrids=[mg])
    if backend == "misocp":
        checked = solve_case_ac_consistent(case, [mg.name], time_limit_seconds=60)
        assert checked.passed, checked.stop_reason
        result = checked.optimization
    else:
        result = solve_case(case, [mg.name], time_limit_seconds=60)
    assert result.success, result.message
    schedules = result.microgrids[mg.name]["resource_schedules"]
    assert {r.resource_id for r in schedules} == {r.resource_id for r in ids}
    svg = next(r for r in schedules if r.resource_type.value == "svg")
    assert max(abs(svg.reactive_power_mvar)) <= min(s, q) + 1e-7
