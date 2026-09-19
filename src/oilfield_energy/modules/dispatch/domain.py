"""Cost equations after application-boundary validation. See dispatch economics v2."""

from math import fsum

from .contracts import CostRates, EconomicCost, RenewableAccounting


def renewable_balance(
    raw: tuple[float, ...], available: tuple[float, ...], used: tuple[float, ...]
) -> RenewableAccounting:
    return RenewableAccounting(
        raw,
        available,
        tuple(max(0.0, r - a) for r, a in zip(raw, available)),
        tuple(max(0.0, a - p) for a, p in zip(available, used)),
    )


def operating_cost(
    price: tuple[float, ...],
    imported: tuple[float, ...],
    curtailed: tuple[float, ...],
    charge: tuple[float, ...],
    discharge: tuple[float, ...],
    loss: tuple[float, ...],
    dt_hours: float,
    rates: CostRates,
) -> EconomicCost:
    components = (
        fsum(p * c for p, c in zip(imported, price)) * dt_hours,
        fsum(curtailed) * dt_hours * rates.curtailment_cny_per_mwh,
        (fsum(charge) + fsum(discharge)) * dt_hours * rates.storage_throughput_cny_per_mwh,
        fsum(loss) * dt_hours * rates.loss_cny_per_mwh,
    )
    return EconomicCost(*components, fsum(components))


def combined_cost(costs: tuple[EconomicCost, ...]) -> EconomicCost:
    components = (
        fsum(c.import_cost_cny for c in costs),
        fsum(c.curtailment_cost_cny for c in costs),
        fsum(c.storage_degradation_cost_cny for c in costs),
        fsum(c.loss_cost_cny for c in costs),
    )
    return EconomicCost(*components, fsum(components))
