"""Pure transitions of absolute-time reserve obligations; never called by solvers."""

# pyright: strict
from typing import Literal
from math import isfinite

from .contracts_reserve import ReserveRecoveryTask


def observe_reserve_recovery(
    previous: ReserveRecoveryTask | None,
    *,
    region: str,
    direction: Literal["up", "down"],
    minute: int,
    required_mw: float,
    recovery_minutes: int,
    adequate: bool | None,
    reason: str = "",
) -> ReserveRecoveryTask | None:
    """Unknown never closes debt; requirement increases never extend its deadline.

    Completed records belong in the caller's immutable event history. An expired
    task remains breached even when a later actual observation closes it.
    """
    if (
        not region
        or direction not in ("up", "down")
        or (adequate is not None and type(adequate) is not bool)
        or type(minute) is not int
        or type(recovery_minutes) is not int
        or minute < 0
        or recovery_minutes <= 0
        or not isfinite(required_mw)
        or required_mw < 0
    ):
        raise ValueError("invalid reserve recovery clock")
    if previous is not None:
        if (
            previous.region != region
            or previous.direction != direction
            or minute < previous.last_observed_minute
        ):
            raise ValueError("reserve observation identity or clock changed")
        if previous.status == "recovered":
            previous = None
    if previous is None:
        if adequate is True:
            return None
        previous = ReserveRecoveryTask(
            region=region,
            direction=direction,
            first_minute=minute,
            deadline_minute=minute + recovery_minutes,
            last_observed_minute=minute,
            required_mw=required_mw,
            status="recovering" if adequate is False else "unknown",
        )
    breach = previous.breached or (
        minute >= previous.deadline_minute and adequate is False
    )
    status = (
        "recovered"
        if adequate is True
        else "violated"
        if breach
        else "unknown"
        if adequate is None
        else "recovering"
    )
    deadline_status = previous.deadline_status
    if breach:
        deadline_status = "missed"
    elif adequate is True:
        deadline_status = "met" if minute <= previous.deadline_minute else "unknown"
    elif minute >= previous.deadline_minute:
        deadline_status = "unknown"
    return previous.model_copy(
        update={
            "last_observed_minute": minute,
            "required_mw": max(previous.required_mw, required_mw),
            "status": status,
            "completed_minute": minute if adequate is True else None,
            "breached": breach,
            "deadline_status": deadline_status,
            "reason": reason,
        }
    )
