"""Reproducible cluster studies on the packaged SC / YA_B / YA_C networks.

The clock is elapsed study time; source_start_minute records the original daily
clock. A window is sliced from 96 quarter-hour samples, NEVER resampled from a
whole day to four points. Captured forecasts are explicitly held within each
quarter hour by the normal execution adapter. No future plant values are used.
"""

from dataclasses import dataclass, field, replace
from math import isfinite

import numpy as np

from oilfield_energy.data import ProjectCase
from oilfield_energy.network_model import validate_legacy_network_alignment
from oilfield_energy.modules.dispatch.contracts import ReserveDeliveryPolicy

from .project_dataset import build_synthetic_case


def mechanism_study_spec(section: str) -> "ClusterStudySpec":
    """Explicit base/override registry for sections 8.3.1 through 8.3.8.

    Reference faults, unavailable optimizers, call sequences and iteration
    budgets are algorithm inputs in the corresponding verification, not hidden
    edits to this physical data. Arithmetic checks use the selected real device.
    """
    if section in {"8.3.1", "8.3.5"}:
        return ClusterStudySpec(low_energy=True)
    if section == "8.3.2":
        return ClusterStudySpec(
            duration_minutes=15, branch_ratings_mva={"SC": {"SC_WIND_LINK": 2.5}}
        )
    if section == "8.3.3":
        return ClusterStudySpec(duration_minutes=15, cluster_import_limit_mw=20.0)
    if section == "8.3.3.1":
        return ClusterStudySpec(
            duration_minutes=15,
            low_energy=True,
            branch_ratings_mva={"SC": {"SC_WIND_LINK": 2.5}},
        )
    if section == "8.3.4":
        return ClusterStudySpec(duration_minutes=15)
    if section == "8.3.6":
        # Full reserve amplitude is unchanged. The call signal's permitted
        # minute-to-minute change is half the commitment, explicitly declared
        # for this slower existing PCS; device ramp and power ratings stay fixed.
        return ClusterStudySpec(activation_slew_fraction_per_minute=0.5)
    if section in {"8.3.7", "8.3.8"}:
        return ClusterStudySpec()
    raise ValueError(f"unknown mechanism section: {section}")


@dataclass(frozen=True)
class ClusterStudySpec:
    source_start_minute: int = 0
    duration_minutes: int = 60
    low_energy: bool = False
    initial_wind_fraction: float = 0.5
    activation_slew_fraction_per_minute: float = 1.0
    # Explicit scenario deltas. Empty maps inherit every base rating/input.
    initial_energy_mwh: dict[str, float] = field(default_factory=dict)
    branch_ratings_mva: dict[str, dict[str, float]] = field(default_factory=dict)
    cluster_import_limit_mw: float | None = None

    def __post_init__(self) -> None:
        if (
            type(self.source_start_minute) is not int
            or type(self.duration_minutes) is not int
            or self.source_start_minute < 0
            or self.duration_minutes <= 0
            or self.source_start_minute % 15
            or self.duration_minutes % 15
            or self.source_start_minute + self.duration_minutes > 1440
        ):
            raise ValueError(
                "study requires a contiguous quarter-hour window within one day"
            )
        if self.cluster_import_limit_mw is not None and (
            not isfinite(self.cluster_import_limit_mw)
            or self.cluster_import_limit_mw <= 0
        ):
            raise ValueError("cluster import limit must be finite and positive")
        if (
            not isfinite(self.initial_wind_fraction)
            or not 0 <= self.initial_wind_fraction <= 1
        ):
            raise ValueError("initial wind fraction must lie in [0, 1]")
        if any(
            not isfinite(v) or v <= 0
            for rows in self.branch_ratings_mva.values()
            for v in rows.values()
        ):
            raise ValueError("branch ratings must be finite and positive")
        if (
            not isfinite(self.activation_slew_fraction_per_minute)
            or not 0 < self.activation_slew_fraction_per_minute <= 1
        ):
            raise ValueError("activation slew fraction must lie in (0, 1]")


def study_delivery_policy(spec: ClusterStudySpec) -> ReserveDeliveryPolicy:
    """Use alongside the case; preserve the explicit scenario's call product."""
    return ReserveDeliveryPolicy(
        activation_slew_fraction_per_minute=spec.activation_slew_fraction_per_minute
    )


def build_cluster_study(spec: ClusterStudySpec | None = None) -> ProjectCase:
    """Keep original equipment, taps, Q permissions, loads and tariff.

    Initial SVG compensation is an explicitly declared pre-existing operating
    state (98% of its apparent rating, clipped to its original Q capability),
    not a zero-time control action. Wind starts at a declared fraction of the
    window's minimum firm forecast (rated 5% error); its Q and PV/storage P/Q
    start at zero. This is a simulated initial condition, not a control action
    using future plant telemetry. Low-energy
    studies start each battery at ITS OWN lower bound, not a shared 0.5 MWh.
    All startup states still require the ordinary joint AC/continuation proof.
    """
    spec = spec or ClusterStudySpec()
    base = build_synthetic_case(96)
    names = {m.name for m in base.microgrids}
    if (set(spec.initial_energy_mwh) | set(spec.branch_ratings_mva)) - names:
        raise ValueError("study overrides contain an unknown region")
    start = spec.source_start_minute // 15
    stop = start + spec.duration_minutes // 15
    grids = []
    for mg in base.microgrids:
        energy = spec.initial_energy_mwh.get(
            mg.name,
            mg.storage.e_min_mwh if spec.low_energy else mg.storage.e_initial_mwh,
        )
        if (
            not isfinite(energy)
            or not mg.storage.e_min_mwh <= energy <= mg.storage.e_max_mwh
        ):
            raise ValueError(f"{mg.name} initial energy is outside physical bounds")
        ratings = spec.branch_ratings_mva.get(mg.name, {})
        if set(ratings) - {line.name for line in mg.lines}:
            raise ValueError(f"{mg.name} branch override contains an unknown branch")
        assert mg.network_model_v2 is not None
        network = replace(
            mg.network_model_v2,
            branches=tuple(
                replace(b, s_max_mva=ratings.get(b.branch_id, b.s_max_mva))
                for b in mg.network_model_v2.branches
            ),
        )
        ids = [r.resource_id for r in mg.resource_identities]
        p = dict.fromkeys(ids, 0.0)
        q = dict.fromkeys(ids, 0.0)
        for bus, values in mg.wind_available_mw.items():
            p[mg.resource_id("wind", bus)] = spec.initial_wind_fraction * max(
                0.0, float(min(values[start:stop])) - 0.05 * mg.wind_capacity_mw[bus]
            )
        svg = mg.svg_capability()
        q[mg.resource_id("svg", mg.svg_bus)] = min(
            svg.effective_q_max_mvar, 0.98 * svg.s_max_mva
        )
        grid = replace(
            mg,
            **{
                key: {
                    bus: values[start:stop].copy()
                    for bus, values in getattr(mg, key).items()
                }
                for key in (
                    "load_p_mw",
                    "load_q_mvar",
                    "wind_available_mw",
                    "pv_available_mw",
                )
            },
            storage=replace(mg.storage, e_initial_mwh=energy),
            lines=[
                replace(line, s_max_mva=ratings.get(line.name, line.s_max_mva))
                for line in mg.lines
            ],
            network_model_v2=network,
            initial_resource_p_mw=p,
            initial_resource_q_mvar=q,
        )
        validate_legacy_network_alignment(grid)
        grids.append(grid)
    return replace(
        base,
        time_hours=np.arange(stop - start, dtype=float) / 4,
        price_cny_per_mwh=base.price_cny_per_mwh[start:stop].copy(),
        microgrids=grids,
        cluster_import_limit_mw=spec.cluster_import_limit_mw
        if spec.cluster_import_limit_mw is not None
        else base.cluster_import_limit_mw,
    )
