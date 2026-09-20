from dataclasses import replace

import pytest

from tests.legacy_case_fixture import build_synthetic_case
from oilfield_energy.modules.resources.contracts import ResourceIdentity, SvgCapability


@pytest.mark.parametrize(
    "s,q,expected", [(1.5, 2.5, 1.5), (2.5, 1.5, 1.5), (2.5, 2.5, 2.5), (1.5, 0.0, 0.0)]
)
def test_svg_intersection_preserves_declared_limits(s, q, expected):
    capability = SvgCapability(-q, q, s)
    assert capability.q_max_mvar == q
    assert capability.s_max_mva == s
    assert capability.effective_q_min_mvar == -expected
    assert capability.effective_q_max_mvar == expected


@pytest.mark.parametrize("args", [(0, 2, 0), (1, 2, 2), (-2, -1, 2), (-2, float("nan"), 2)])
def test_invalid_capabilities_rejected(args):
    with pytest.raises(ValueError):
        SvgCapability(*args)


def test_identity_mapping_requires_complete_unique_resources():
    mg = build_synthetic_case(steps=4).microgrids[0]
    keys = (
        [("wind", b) for b in mg.wind_available_mw]
        + [("pv", b) for b in mg.pv_available_mw]
        + [("storage", mg.storage.bus), ("svg", mg.svg_bus)]
    )
    ids = tuple(ResourceIdentity(f"custom-{i}", bus, kind) for i, (kind, bus) in enumerate(keys))
    mapped = replace(mg, resource_identities=ids)
    assert mapped.resource_id(*keys[0]) == "custom-0"
    for bad in (
        ids[:-1],
        ids + (ids[0],),
        tuple(replace(i, resource_id="duplicate") for i in ids),
    ):
        with pytest.raises(ValueError):
            replace(mg, resource_identities=bad)
