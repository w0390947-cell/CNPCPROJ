"""Snapshot capture use case for complete ADMM updates; no I/O or solver SDK."""

from collections.abc import Iterable, Sequence
from dataclasses import replace
from math import hypot, isfinite, sqrt

from .contracts import (
    CoordinationResiduals,
    CoordinationSnapshot,
    DispatchCapabilities,
    RegionalPlanSnapshot,
)


def _matrix(values: Iterable[Iterable[float]]) -> tuple[tuple[float, ...], ...]:
    rows = tuple(tuple(float(v) for v in row) for row in values)
    if not rows or not rows[0] or len({len(row) for row in rows}) != 1:
        raise ValueError("coordination matrices must be nonempty and rectangular")
    if any(not isfinite(v) for row in rows for v in row):
        raise ValueError("coordination matrices must be finite")
    return rows


def capture_coordination_snapshot(
    *,
    epoch: int,
    communication_tick: int,
    capabilities: DispatchCapabilities,
    regions: Sequence[RegionalPlanSnapshot],
    p_references_mw: Iterable[Iterable[float]],
    q_references_mvar: Iterable[Iterable[float]],
    previous_p_references_mw: Iterable[Iterable[float]],
    previous_q_references_mvar: Iterable[Iterable[float]],
    dual_p: Iterable[Iterable[float]],
    dual_q: Iterable[Iterable[float]],
    rho: float,
    absolute_tolerance: float,
    relative_tolerance: float,
) -> CoordinationSnapshot:
    """Copy and bind a complete local-plan/reference/dual/cost snapshot.

    Raises ValueError on partial/mixed epochs, invalid dimensions or numerics.
    A snapshot starts un-certified; the orchestrator marks it converged only
    after its configured consecutive complete-update stopping rule succeeds.
    """
    if epoch < 0 or communication_tick < 1:
        raise ValueError("invalid coordination epoch or communication tick")
    if (
        not all(isfinite(v) for v in (rho, absolute_tolerance, relative_tolerance))
        or rho <= 0
        or absolute_tolerance < 0
        or relative_tolerance < 0
    ):
        raise ValueError("invalid ADMM scaling or residual tolerances")
    if not regions or len({r.name for r in regions}) != len(regions):
        raise ValueError("complete unique regional plans required")
    if any(not r.name or r.signal_iteration != epoch or r.used_fallback for r in regions):
        raise ValueError("snapshot requires coordinated plans from one epoch")
    matrices = tuple(
        _matrix(m)
        for m in (
            (r.p_grid_mw for r in regions),
            (r.q_grid_mvar for r in regions),
            p_references_mw,
            q_references_mvar,
            previous_p_references_mw,
            previous_q_references_mvar,
            dual_p,
            dual_q,
        )
    )
    if len({(len(m), len(m[0])) for m in matrices}) != 1:
        raise ValueError("all coordination matrix shapes must match region/time axes")
    x_p, x_q, z_p, z_q, old_p, old_q, u_p, u_q = matrices
    size = len(x_p[0])
    owned: list[RegionalPlanSnapshot] = []
    for r in regions:
        profiles = _matrix(
            (r.renewable_used_mw, r.storage_charge_mw, r.storage_discharge_mw, r.q_support_mvar)
        )
        energy = tuple(float(v) for v in r.storage_energy_mwh)
        if len(profiles[0]) != size or len(energy) != size + 1:
            raise ValueError("regional resource dimensions differ from PCC plan")
        if not all(isfinite(v) for v in energy) or not isfinite(r.local_objective_cny):
            raise ValueError("regional cost and storage state must be finite")
        if not capabilities.storage_enabled and (
            any(abs(v) > 2e-5 for row in profiles[1:3] for v in row)
            or any(abs(v - energy[0]) > 2e-5 for v in energy)
        ):
            raise ValueError("disabled storage participated in coordinated plan")
        owned.append(
            replace(
                r,
                p_grid_mw=tuple(float(v) for v in r.p_grid_mw),
                q_grid_mvar=tuple(float(v) for v in r.q_grid_mvar),
                renewable_used_mw=profiles[0],
                storage_charge_mw=profiles[1],
                storage_discharge_mw=profiles[2],
                q_support_mvar=profiles[3],
                storage_energy_mwh=energy,
            )
        )

    def flat(*items: tuple[tuple[float, ...], ...]) -> tuple[float, ...]:
        return tuple(v for m in items for row in m for v in row)

    x, z, old, u = flat(x_p, x_q), flat(z_p, z_q), flat(old_p, old_q), flat(u_p, u_q)
    residuals = CoordinationResiduals(
        primal=hypot(*(a - b for a, b in zip(x, z))),
        dual=rho * hypot(*(a - b for a, b in zip(z, old))),
        primal_tolerance=sqrt(len(x)) * absolute_tolerance
        + relative_tolerance * max(hypot(*x), hypot(*z)),
        dual_tolerance=sqrt(len(x)) * absolute_tolerance + relative_tolerance * rho * hypot(*u),
    )
    if not all(
        isfinite(v)
        for v in (
            residuals.primal,
            residuals.dual,
            residuals.primal_tolerance,
            residuals.dual_tolerance,
        )
    ):
        raise ValueError("coordination residual arithmetic overflowed")
    return CoordinationSnapshot(
        epoch,
        communication_tick,
        False,
        capabilities,
        tuple(owned),
        z_p,
        z_q,
        old_p,
        old_q,
        u_p,
        u_q,
        rho,
        residuals,
    )
