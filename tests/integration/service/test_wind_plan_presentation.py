"""Public wind Q is the signed optimized regional total, never a capability estimate."""

import numpy as np
import pytest

from oilfield_energy import service


@pytest.mark.parametrize("formulation", ["legacy_milp", "misocp"])
@pytest.mark.parametrize("region", ["SC", "YA_B"])
def test_service_exposes_selected_regions_optimized_wind_q(monkeypatch, formulation, region):
    original_solve = service._solve
    solved = []

    def capture(*args, **kwargs):
        result = original_solve(*args, **kwargs)
        solved.append(result[0])
        return result

    monkeypatch.setattr(service, "_solve", capture)
    result = service.run_simulation(
        service.SimulationRequest.model_validate(
            {
                "region": region,
                "steps": 4,
                "solver": {"formulation": formulation},
            }
        )
    )
    optimized = solved[-1].microgrids[region]
    actual = [p.wind_q_mvar for p in result.timeseries]
    np.testing.assert_allclose(actual, optimized["wind_q_mvar"], atol=1e-9)
    np.testing.assert_allclose(
        actual,
        np.sum(list(optimized["wind_reactive_by_bus_mvar"].values()), axis=0),
        atol=1e-9,
    )
    payload = result.model_dump(mode="json")
    for point, q in zip(payload["timeseries"], [-0.6, 0.0, 0.6, None], strict=True):
        point["wind_q_mvar"] = q
    restored = service.SimulationResult.model_validate(payload)
    assert [p.wind_q_mvar for p in restored.timeseries] == [-0.6, 0.0, 0.6, None]
    for point in payload["timeseries"]:
        point.pop("wind_q_mvar")
    historical = service.SimulationResult.model_validate(payload)
    assert all(p.wind_q_mvar is None for p in historical.timeseries)
    assert all("wind_q_mvar" not in point for point in payload["timeseries"])
