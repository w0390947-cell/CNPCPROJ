"""ProjectCase projection for the public planning reserve contract."""

from dataclasses import replace
from collections.abc import Mapping

import numpy as np

from oilfield_energy.data import MicrogridData, ProjectCase
from oilfield_energy.modules.dispatch.api import plan_storage_reserve
from oilfield_energy.modules.dispatch.contracts import (
    StorageReservePlan,
    StorageReservePolicy,
    ReserveRecoveryTask,
)


def with_storage_reserves(
    case: ProjectCase,
    policy: StorageReservePolicy,
    remaining_day_minutes: int,
    *,
    start_minute: int = 0,
    tasks: Mapping[tuple[str, str], ReserveRecoveryTask] | None = None,
    full_support: bool = False,
    delivery_alpha: float | None = None,
    delivery_ramp_mw: float | None = None,
    delivery_headroom_factor: float = 1.0,
) -> ProjectCase:
    reserves: dict[str, StorageReservePlan] = {}
    microgrids: list[MicrogridData] = []
    for mg in case.microgrids:
        debts = [
            t
            for t in (tasks or {}).values()
            if t.region == mg.name and t.status != "recovered"
        ]
        st = mg.storage
        scale = sum(
            (
                *mg.load_p_mw.values(),
                *mg.wind_available_mw.values(),
                *mg.pv_available_mw.values(),
            ),
            np.zeros(len(case.time_hours)),
        )
        reserve = plan_storage_reserve(
            forecast_scale_mw=tuple(map(float, scale)),
            step_minutes=case.assumptions.dt_hours * 60,
            remaining_day_minutes=remaining_day_minutes,
            initial_energy_mwh=st.e_initial_mwh,
            minimum_energy_mwh=st.e_min_mwh,
            maximum_energy_mwh=st.e_max_mwh,
            maximum_power_mw=st.p_max_mw,
            eta_charge=st.eta_charge,
            eta_discharge=st.eta_discharge,
            policy=policy,
            recovery_remaining_minutes=max(
                0, min((t.deadline_minute - start_minute for t in debts), default=0)
            )
            if tasks is not None
            else None,
            full_support=full_support,
            required_floor_mw=max((t.required_mw for t in debts), default=0.0),
            delivery_alpha=delivery_alpha,
            delivery_ramp_mw=delivery_ramp_mw,
            delivery_headroom_factor=delivery_headroom_factor,
        )
        reserves[mg.name] = reserve
        # The day-ahead endpoint is a reference, not permission to consume reserve.
        # The caller records both the original and the adopted endpoint.
        terminal = min(
            reserve.energy_ceiling_mwh[-1],
            max(
                reserve.energy_floor_mwh[-1],
                st.terminal_energy_mwh,
            ),
        )
        microgrids.append(replace(mg, storage=replace(st, e_terminal_mwh=terminal)))
    return replace(case, microgrids=microgrids, storage_reserves=reserves)
