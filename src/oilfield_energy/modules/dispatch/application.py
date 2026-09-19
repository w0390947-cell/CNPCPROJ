"""Validated, deterministic accounting use cases; inputs are never modified."""

from collections.abc import Iterable
from math import fsum, isclose, isfinite

from .contracts import CostRates, EconomicCost, ReferenceEconomics, RenewableAccounting
from .domain import combined_cost, operating_cost, renewable_balance

# Solver-output roundoff allowance in MW, not an engineering feasibility limit.
POWER_ROUNDOFF_MW = 2e-5


def _validate_cost(cost: EconomicCost) -> None:
    components = (
        cost.import_cost_cny,
        cost.curtailment_cost_cny,
        cost.storage_degradation_cost_cny,
        cost.loss_cost_cny,
    )
    if not all(isfinite(v) for v in (*components, cost.economic_cost_cny)):
        raise ValueError("economic cost must be finite")
    if any(v < 0 for v in components[1:]) or not isclose(
        fsum(components),
        cost.economic_cost_cny,
        rel_tol=1e-12,
        abs_tol=1e-6,
    ):
        raise ValueError("economic cost components are inconsistent")


def _profile(values: Iterable[float], *, nonnegative: bool = True) -> tuple[float, ...]:
    profile = tuple(float(v) for v in values)
    if not profile or not all(isfinite(v) for v in profile):
        raise ValueError("accounting profiles must be nonempty and finite")
    if nonnegative:
        if any(v < -POWER_ROUNDOFF_MW for v in profile):
            raise ValueError("negative physical power in accounting profile")
        profile = tuple(max(0.0, v) for v in profile)
    return profile


def account_renewable(
    raw_available_mw: Iterable[float],
    available_mw: Iterable[float],
    dispatched_mw: Iterable[float],
) -> RenewableAccounting:
    """Split rated-MW clipping from dispatch curtailment; reject invalid balances.

    Availability must already include each resource's rated-MW cap. Aggregate
    only AFTER applying per-resource caps; this function never infers asset IDs.
    """
    raw = _profile(raw_available_mw, nonnegative=False)
    available = _profile(available_mw, nonnegative=False)
    used = _profile(dispatched_mw)
    if any(v < 0 for v in (*raw, *available)):
        raise ValueError("forecast availability must be nonnegative without roundoff repair")
    if len({len(raw), len(available), len(used)}) != 1:
        raise ValueError("renewable accounting profile lengths differ")
    if any(
        a > r + POWER_ROUNDOFF_MW or p > a + POWER_ROUNDOFF_MW
        for r, a, p in zip(raw, available, used)
    ):
        raise ValueError("renewable accounting exceeds raw or rated availability")
    return renewable_balance(raw, available, used)


def evaluate_economics(
    *,
    price_cny_per_mwh: Iterable[float],
    import_mw: Iterable[float],
    curtailed_mw: Iterable[float],
    charge_mw: Iterable[float],
    discharge_mw: Iterable[float],
    loss_mw: Iterable[float],
    dt_hours: float,
    rates: CostRates,
) -> EconomicCost:
    """Return declared weighted operating cost, not a literal electricity bill.

    All profiles share chronological interval order and length; dt_hours is a
    positive constant interval duration. Negative electricity prices are valid.
    Nonfinite, mismatched or materially negative power inputs raise ValueError.
    Artificial voltage, tracking and regularization penalties are excluded.
    """
    if not isfinite(dt_hours) or dt_hours <= 0:
        raise ValueError("dt_hours must be finite and positive")
    if any(
        not isfinite(v) or v < 0
        for v in (
            rates.curtailment_cny_per_mwh,
            rates.storage_throughput_cny_per_mwh,
            rates.loss_cny_per_mwh,
        )
    ):
        raise ValueError("economic rates must be finite and nonnegative")
    price = _profile(price_cny_per_mwh, nonnegative=False)
    powers = tuple(_profile(p) for p in (import_mw, curtailed_mw, charge_mw, discharge_mw, loss_mw))
    if any(len(p) != len(price) for p in powers):
        raise ValueError("economic accounting profile lengths differ")
    imported, curtailed, charge, discharge, loss = powers
    try:
        cost = operating_cost(price, imported, curtailed, charge, discharge, loss, dt_hours, rates)
        _validate_cost(cost)
    except OverflowError as exc:
        raise ValueError("economic cost exceeds finite numeric range") from exc
    return cost


def assess_reference_economics(
    costs: Iterable[EconomicCost],
    *,
    centralized_economic_cost_cny: float,
    surrogate_objective_cny: float,
    converged: bool,
) -> ReferenceEconomics:
    """Compare aligned aggregate costs; undefined/unconverged gaps stay null."""
    items = tuple(costs)
    if not items or not all(
        isfinite(v)
        for v in (
            centralized_economic_cost_cny,
            surrogate_objective_cny,
            *(
                v
                for c in items
                for v in (
                    c.import_cost_cny,
                    c.curtailment_cost_cny,
                    c.storage_degradation_cost_cny,
                    c.loss_cost_cny,
                    c.economic_cost_cny,
                )
            ),
        )
    ):
        raise ValueError("reference accounting requires finite costs")
    try:
        for item in items:
            _validate_cost(item)
        cost = combined_cost(items)
        _validate_cost(cost)
    except OverflowError as exc:
        raise ValueError("combined cost exceeds finite numeric range") from exc
    status = "comparable" if converged else "not_converged"
    gap = None
    if converged and centralized_economic_cost_cny <= 0:
        status = "nonpositive_baseline"
    elif converged:
        gap = (
            100
            * (cost.economic_cost_cny - centralized_economic_cost_cny)
            / centralized_economic_cost_cny
        )
        if not isfinite(gap):
            raise ValueError("economic gap exceeds finite numeric range")
    return ReferenceEconomics(
        cost, centralized_economic_cost_cny, surrogate_objective_cny, gap, status
    )
