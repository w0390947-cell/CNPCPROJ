"""Sparse nominal P/Q dynamics shared by the solver and independent assessment."""

from dataclasses import dataclass
from fractions import Fraction as F
from math import cos, pi, sin
from typing import Literal

from .contracts_pq import PQPlanningInput, PQPlanningWitness

Axis = Literal["p", "q", "up", "uq", "dq"]


@dataclass(frozen=True)
class PQTerm:
    resource: int
    window: int
    step: int
    axis: Axis
    coefficient: float


@dataclass(frozen=True)
class PQRow:
    terms: tuple[PQTerm, ...]
    upper: float
    equality: bool = False


def pq_planning_rows(x: PQPlanningInput) -> tuple[PQRow, ...]:
    """Five-minute default is a construction horizon, not a tracking deadline.

    Every state and command uses its own minute boundary. Matched P/Q responses
    traverse convex segments; unmatched responses use conservative rectangles.
    PCS P is held for the entire economic interval, preserving its energy model.
    """
    rows: list[PQRow] = []
    h, m = x.transition_minutes, x.planning_minutes

    def add(terms: list[PQTerm], upper: float, equality: bool = False) -> None:
        # Substitute the real initial state or the preceding window's endpoint.
        resolved: list[PQTerm] = []
        for term in terms:
            if term.step == 0 and term.axis in ("p", "q"):
                if term.window == 0:
                    values = x.initial.p_mw if term.axis == "p" else x.initial.q_mvar
                    upper -= term.coefficient * values[term.resource]
                    continue
                term = PQTerm(
                    term.resource, term.window - 1, h, term.axis, term.coefficient
                )
            resolved.append(term)
        rows.append(PQRow(tuple(resolved), upper, equality))

    for t in range(x.planning_steps):
        for i, r in enumerate(x.devices.resources):

            def term(k: int, axis: Axis, c: float = 1.0) -> PQTerm:
                return PQTerm(i, t, k, axis, c)

            facets = [(0.0, 1.0, r.q.high), (0.0, -1.0, -r.q.low)]
            if r.q_over_p is not None:
                facets += [(-r.q_over_p, 1.0, 0.0), (-r.q_over_p, -1.0, 0.0)]
            # The disk remains exact in the execution certificate. This inner
            # polygon is only a nominal economic construction, with its own ID.
            facets += (
                [(0.0, 1.0, r.s_mva), (0.0, -1.0, r.s_mva)]
                if r.kind == "svg"
                else [
                    (
                        cos(2 * pi * j / x.polygon_sides),
                        sin(2 * pi * j / x.polygon_sides),
                        r.s_mva * cos(pi / x.polygon_sides),
                    )
                    for j in range(x.polygon_sides)
                ]
            )
            for k in range(1, h + 1):
                dynamics: tuple[tuple[Axis, Axis, float], ...] = (
                    ("p", "up", r.alpha_p),
                    ("q", "uq", x.devices.alpha_q),
                )
                for axis, command, alpha in dynamics:
                    zero_active = r.kind in ("wind", "pv") and (
                        x.p_ceiling_mw[t * m + k][i] == 0
                        or (
                            k == h
                            and min(
                                x.p_ceiling_mw[t * m + j][i] for j in range(h, m + 1)
                            )
                            == 0
                        )
                    )
                    zero_axis = zero_active and (axis == "p" or r.q_over_p is not None)
                    if zero_axis:
                        # A declared zero ceiling fixes this branch: command and
                        # state are zero, but only if the exact device settling
                        # condition permits it. No limiter or capability is relaxed.
                        add([term(k, axis)], 0, True)
                        add([term(k, command)], 0, True)
                        for sign in (-1, 1):
                            add(
                                [term(k - 1, axis, sign * (1 - alpha))],
                                x.settling_utilization * x.devices.settling_resolution,
                            )
                        continue
                    add(
                        [
                            term(k, axis),
                            term(k - 1, axis, -(1 - alpha)),
                            term(k, command, -alpha),
                        ],
                        0,
                        True,
                    )
                boundaries: tuple[tuple[Axis, int], ...] = (
                    ("up", t * m + k - 1),
                    ("p", t * m + k),
                )
                for axis, minute in boundaries:
                    add([term(k, axis)], x.p_ceiling_mw[minute][i])
                    add([term(k, axis, -1)], -r.p.low)
                add(
                    [term(k, "p"), term(k - 1, "p", -1)],
                    x.ramp_utilization * r.ramp_p_mw,
                )
                add(
                    [term(k, "p", -1), term(k - 1, "p")],
                    x.ramp_utilization * r.ramp_p_mw,
                )
                add([term(k, "q"), term(k - 1, "q", -1), term(k, "dq", -1)], 0)
                add([term(k, "q", -1), term(k - 1, "q"), term(k, "dq", -1)], 0)
                for a, b, limit in facets:
                    add([term(k, "up", a), term(k, "uq", b)], limit)
                    add([term(k, "p", a), term(k, "q", b)], limit)
                    if r.alpha_p != x.devices.alpha_q:
                        add([term(k - 1, "p", a), term(k, "q", b)], limit)
                        add([term(k, "p", a), term(k - 1, "q", b)], limit)
                if r.kind == "storage":
                    add([term(k, "p"), term(h, "p", -1)], 0, True)
            # The endpoint is held over the remainder; all future minute caps,
            # including the next interval boundary, must therefore allow it.
            add(
                [term(h, "p")],
                min(x.p_ceiling_mw[t * m + k][i] for k in range(h, m + 1)),
            )
            for a, b, limit in facets:
                add(
                    [term(h, "p", a), term(h, "q", b / (1 - x.reserve_fraction))], limit
                )
        for k in range(1, h + 1):
            add(
                [PQTerm(i, t, k, "dq", 1) for i in range(len(x.devices.resources))],
                x.ramp_utilization * x.devices.aggregate_q_ramp_mvar,
            )
    return tuple(rows)


def pq_witness_violation(w: PQPlanningWitness) -> float:
    """Reconstruct nominal states, then evaluate all rows independently of SCIP.

    This is nominal dynamics evidence, never a robust AC or tracking certificate.
    """
    x = w.inputs
    values: dict[tuple[int, int, int, Axis], float] = {}
    endpoint_residual = 0.0
    ps, qs = x.initial.p_mw, x.initial.q_mvar
    for t, commands in enumerate(w.commands):
        for k, command in enumerate(commands, 1):

            def response(
                i: int, source: float, u: float, alpha: float, reactive: bool
            ) -> float:
                r = x.devices.resources[i]
                z = (1 - F(alpha)) * F(source) + F(alpha) * F(u)
                zero = r.kind in ("wind", "pv") and (
                    x.p_ceiling_mw[t * x.planning_minutes + k][i] == 0
                    or (
                        k == x.transition_minutes
                        and min(
                            x.p_ceiling_mw[t * x.planning_minutes + j][i]
                            for j in range(x.transition_minutes, x.planning_minutes + 1)
                        )
                        == 0
                    )
                )
                if (
                    zero
                    and (not reactive or r.q_over_p is not None)
                    and u == 0
                    and abs(z) <= F(x.devices.settling_resolution)
                ):
                    return 0.0
                return float(z)

            next_p = tuple(
                response(i, p, u, r.alpha_p, False)
                for i, (r, p, u) in enumerate(
                    zip(x.devices.resources, ps, command.p_mw, strict=True)
                )
            )
            next_q = tuple(
                response(i, q, u, x.devices.alpha_q, True)
                for i, (q, u) in enumerate(zip(qs, command.q_mvar, strict=True))
            )
            for i in range(len(ps)):
                axes: tuple[tuple[Axis, float], ...] = (
                    ("p", next_p[i]),
                    ("q", next_q[i]),
                    ("up", command.p_mw[i]),
                    ("uq", command.q_mvar[i]),
                    ("dq", abs(next_q[i] - qs[i])),
                )
                for axis, value in axes:
                    values[i, t, k, axis] = value
            ps, qs = next_p, next_q
        endpoint = w.endpoints[t]
        endpoint_residual = max(
            endpoint_residual,
            *(abs(a - b) for a, b in zip(ps, endpoint.p_mw, strict=True)),
            *(abs(a - b) for a, b in zip(qs, endpoint.q_mvar, strict=True)),
        )
        for i in range(len(ps)):
            # Endpoint equality is independently checked against the recurrence.
            values[i, t, x.transition_minutes, "p"] = endpoint.p_mw[i]
            values[i, t, x.transition_minutes, "q"] = endpoint.q_mvar[i]
        ps, qs = endpoint.p_mw, endpoint.q_mvar
    return max(
        0.0,
        endpoint_residual,
        *(
            abs(delta) if row.equality else delta
            for row in pq_planning_rows(x)
            for delta in [
                sum(
                    term.coefficient
                    * values[term.resource, term.window, term.step, term.axis]
                    for term in row.terms
                )
                - row.upper
            ]
        ),
    )
