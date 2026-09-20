from oilfield_energy.service import ADMMHistoryPoint


def test_historical_history_preserves_unknown_power_evidence():
    old = ADMMHistoryPoint(
        iteration=1,
        primal_residual=1,
        dual_residual=1,
        primal_tolerance=0.1,
        dual_tolerance=0.1,
        fresh_region_count=3,
        convergence_streak=0,
        fallback_regions=[],
    )
    assert old.coordination is None
    assert (
        ADMMHistoryPoint.model_validate_json(old.model_dump_json()).coordination is None
    )
