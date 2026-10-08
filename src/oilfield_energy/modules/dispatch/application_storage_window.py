"""Necessary terminal reachability bounds, never a sufficient safety proof."""

from fractions import Fraction as F

from oilfield_energy.kernel.enclosures import lower, upper

from .contracts_storage_window import StorageWindowTarget


def storage_window_target(
    *,
    minimum_mwh: float,
    maximum_mwh: float,
    task_minimum_mwh: float,
    task_maximum_mwh: float,
    remaining_hours: float,
    charge_limit_mw: float,
    discharge_limit_mw: float,
    eta_charge: float,
    eta_discharge: float,
    reference_mwh: float,
    reference_cost_cny_per_mwh: float,
) -> StorageWindowTarget:
    if remaining_hours < 0 or min(charge_limit_mw, discharge_limit_mw) < 0:
        raise ValueError("negative remaining horizon or power limit")
    if not 0 < eta_charge <= 1 or not 0 < eta_discharge <= 1:
        raise ValueError("invalid storage efficiency")
    return StorageWindowTarget(
        minimum_mwh=max(
            minimum_mwh,
            lower(
                F(task_minimum_mwh)
                - F(charge_limit_mw) * F(remaining_hours) * F(eta_charge)
            ),
        ),
        maximum_mwh=min(
            maximum_mwh,
            upper(
                F(task_maximum_mwh)
                + F(discharge_limit_mw) * F(remaining_hours) / F(eta_discharge)
            ),
        ),
        reference_mwh=reference_mwh,
        reference_cost_cny_per_mwh=reference_cost_cny_per_mwh,
    )
