"""Constructive joint safety induction: certified prefix, return, invariant set.

All quantifiers are over the frozen declared disturbance domain. A failed
construction is unproven, never evidence that the physical problem has no solution.
"""

# pyright: strict
from hashlib import sha256
from math import sqrt
from oilfield_energy.modules.control.api import (
    certify_cluster_device_transition,
    approach_device_command,
    point_box,
    validate_device_box,
)
from oilfield_energy.modules.control.contracts import TubeBox, TubeCommand, TubeState
from .contracts_safety import (
    ClusterSafetyDomain,
    ClusterRegionDomain,
    ClusterContinuation,
    JointPathCertificate,
)
from .safety_network import certify_joint_boxes, model_at
from .safety_reserve import certify_state_reserve, energy_band


def midpoint(box: TubeBox) -> TubeState:
    return TubeState(
        p_mw=tuple((x.low + x.high) / 2 for x in box.p_mw),
        q_mvar=tuple((x.low + x.high) / 2 for x in box.q_mvar),
        energy_mwh=(box.energy_mwh.low + box.energy_mwh.high) / 2,
    )


def certify_step(
    domain: ClusterSafetyDomain,
    boxes: tuple[TubeBox, ...],
    commands: tuple[TubeCommand, ...],
    minute: int,
) -> JointPathCertificate:
    if len(boxes) != len(domain.regions) or len(commands) != len(boxes):
        raise ValueError("joint command region axis differs")
    transitions = tuple(
        certify_cluster_device_transition(model_at(r, minute), box, u)
        for r, box, u in zip(domain.regions, boxes, commands, strict=True)
    )
    endpoints = tuple(t.endpoint for t in transitions)
    for r, box in zip(domain.regions, endpoints, strict=True):
        validate_device_box(model_at(r, minute + 1), box)
    paths = tuple(t.path for t in transitions)
    networks, p, q = certify_joint_boxes(domain, paths, minute)
    if domain.reserve_policy is not None and minute + 1 >= min(
        r.recovery_deadline_minute for r in domain.regions
    ):
        reserve = certify_state_reserve(domain, endpoints, minute + 1)
        if reserve is None or reserve.status != "certified":
            raise ValueError(
                "original reserve deadline/continuation unproven: "
                + (reserve.reason if reserve else "missing")
            )
    return JointPathCertificate(
        minute=minute,
        endpoints=endpoints,
        paths=paths,
        networks=networks,
        pcc_p_mw=p,
        pcc_q_mvar=q,
    )


def terminal_boxes(
    domain: ClusterSafetyDomain, commands: tuple[TubeCommand, ...]
) -> tuple[TubeBox, ...]:
    return tuple(
        point_box(
            TubeState(
                p_mw=u.p_mw, q_mvar=u.q_mvar, energy_mwh=energy_band(domain, i).low
            )
        ).model_copy(update={"energy_mwh": energy_band(domain, i)})
        for i, u in enumerate(commands)
    )


def certify_terminal(
    domain: ClusterSafetyDomain, commands: tuple[TubeCommand, ...]
) -> str:
    boxes = terminal_boxes(domain, commands)
    if any(
        u.p_mw[
            next(i for i, x in enumerate(r.devices.resources) if x.kind == "storage")
        ]
        != 0
        for r, u in zip(domain.regions, commands, strict=True)
    ):
        raise ValueError("finite storage cannot sustain nonzero terminal active power")
    digest = sha256(domain.context_id.encode())
    seen: set[str] = set()
    for k in range(len(domain.regions[0].forecasts)):
        identity = "".join(r.forecasts[k].model_dump_json() for r in domain.regions)
        # Reserve covers the next full support tail as well as this minute.
        duration = (
            domain.reserve_policy.support_minutes + 1 if domain.reserve_policy else 0
        )
        identity += "".join(
            r.forecasts[min(k + j, len(r.forecasts) - 1)].model_dump_json()
            for r in domain.regions
            for j in range(1, duration + 1)
        )
        if identity in seen:
            continue
        seen.add(identity)
        step = certify_step(domain, boxes, commands, k)
        if step.endpoints != boxes:
            raise ValueError("joint terminal set is not invariant")
        reserve = certify_state_reserve(domain, boxes, k)
        if reserve is not None and reserve.status != "certified":
            raise ValueError("terminal reserve unproven: " + reserve.reason)
        digest.update(step.model_dump_json().encode())
    return digest.hexdigest()


def construct_initial_continuation(
    domain: ClusterSafetyDomain,
) -> tuple[tuple[TubeCommand, ...], str, ClusterContinuation]:
    """Select a certified terminal AND a reachable initial path as one pair.

    A safe terminal alone need not be reachable under the original recovery
    deadline and cluster import limit. Try the remaining declared candidates
    when a path fails; never change a limit or reset the initial state.
    """
    failures: list[str] = []
    for fraction in (0.0, 0.25, 0.5, 0.75, 1.0):
        commands: list[TubeCommand] = []
        for r in domain.regions:
            p = [
                fraction * min(f.available_mw[i] for f in r.forecasts)
                if x.kind in ("wind", "pv")
                else 0.0
                for i, x in enumerate(r.devices.resources)
            ]
            q = [0.0] * len(p)
            demand = sum(
                sum((x.low + x.high) / 2 for x in f.load_q_mvar) for f in r.forecasts
            ) / len(r.forecasts) - sum(r.network.shunt_q_mvar)
            for i in sorted(
                range(len(p)),
                key=lambda i: 0 if r.devices.resources[i].kind == "svg" else 1,
            ):
                x = r.devices.resources[i]
                cap = sqrt(max(0.0, x.s_mva * x.s_mva - p[i] * p[i])) * 0.98
                if x.q_over_p is not None:
                    cap = min(cap, x.q_over_p * p[i] * 0.98)
                q[i] = min(x.q.high, cap, max(x.q.low, -cap, demand))
                demand -= q[i]
            commands.append(TubeCommand(p_mw=tuple(p), q_mvar=tuple(q)))
        try:
            selected = tuple(commands)
            digest = certify_terminal(domain, selected)
            path = certify_continuation(
                domain,
                tuple(point_box(r.initial) for r in domain.regions),
                0,
                selected,
                digest,
            )
            return selected, digest, path
        except ValueError as exc:
            failures.append(str(exc))
    raise ValueError(
        "joint initial continuation construction unproven: "
        + "; ".join(dict.fromkeys(failures))
    )


def settled(
    domain: ClusterSafetyDomain,
    boxes: tuple[TubeBox, ...],
    commands: tuple[TubeCommand, ...],
) -> bool:
    return all(
        all(
            x.low == x.high == v
            for x, v in zip((*b.p_mw, *b.q_mvar), (*u.p_mw, *u.q_mvar), strict=True)
        )
        and b.energy_mwh.low >= energy_band(domain, i).low
        and b.energy_mwh.high <= energy_band(domain, i).high
        for i, (b, u) in enumerate(zip(boxes, commands, strict=True))
    )


def stopping_energy(region: ClusterRegionDomain, power: float) -> float:
    """Candidate-only estimate of energy during bounded PCS braking.

    This does not certify a path. It prevents the return heuristic from charging
    up to its energy target and only then noticing that the lagged PCS needs
    several more minutes to stop. Every chosen step and deadline is still proved.
    """
    model = region.devices
    storage = next(x for x in model.resources if x.kind == "storage")
    energy = 0.0
    for _ in range(120):
        if abs(power) < 1e-12:
            break
        alpha = storage.alpha_p
        radius = 0.98 * storage.ramp_p_mw / alpha
        command = max(
            storage.p.low,
            power - radius,
            min(storage.p.high, power + radius, -(1 - alpha) * power / alpha),
        )
        after = (1 - alpha) * power + alpha * command
        average = (power + after) / 2
        energy -= (
            average / model.eta_discharge
            if average >= 0
            else average * model.eta_charge
        ) / 60
        power = after
    return energy


def certify_continuation(
    domain: ClusterSafetyDomain,
    initial: tuple[TubeBox, ...],
    start: int,
    terminal: tuple[TubeCommand, ...],
    terminal_digest: str,
    proposal: tuple[TubeCommand, ...] | None = None,
) -> ClusterContinuation:
    boxes = initial
    commands: list[tuple[TubeCommand, ...]] = []
    steps: list[JointPathCertificate] = []
    for offset in range(domain.policy.maximum_return_minutes + 1):
        if (offset or proposal is None) and settled(domain, boxes, terminal):
            return ClusterContinuation(
                context_id=domain.context_id,
                start_minute=start,
                initial=initial,
                commands=tuple(commands),
                steps=tuple(steps),
                terminal=boxes,
                terminal_commands=terminal,
                terminal_proof_digest=terminal_digest,
            )
        if offset == domain.policy.maximum_return_minutes:
            break
        minute = start + offset
        failure = "no certified return movement"
        for fraction in (
            (1.0,)
            if offset == 0 and proposal is not None
            else (1.0, 0.5, 0.25, 0.125, 0.0625)
        ):
            choices: list[TubeCommand] = []
            for i, (r, b, u) in enumerate(
                zip(domain.regions, boxes, terminal, strict=True)
            ):
                if offset == 0 and proposal is not None:
                    choice = proposal[i]
                else:
                    p = list(u.p_mw)
                    j = next(
                        j
                        for j, x in enumerate(r.devices.resources)
                        if x.kind == "storage"
                    )
                    band = energy_band(domain, i)
                    deficit = band.low - b.energy_mwh.low
                    excess = b.energy_mwh.high - band.high
                    current_power = midpoint(b).p_mw[j]
                    braking = stopping_energy(r, current_power)
                    can_brake = (
                        b.energy_mwh.low + braking >= band.low
                        and b.energy_mwh.high + braking <= band.high
                    )
                    remaining = max(1, r.recovery_deadline_minute - minute - 4)
                    if can_brake and abs(current_power) > 1e-10:
                        alpha = r.devices.resources[j].alpha_p
                        p[j] = -(1 - alpha) * current_power / alpha
                    elif deficit > 0:
                        p[j] = -min(
                            -r.devices.resources[j].p.low,
                            1.3
                            * (deficit + 0.002)
                            * 60
                            / (remaining * r.devices.eta_charge),
                        )
                    elif excess > 0:
                        p[j] = min(
                            r.devices.resources[j].p.high,
                            1.3
                            * (excess + 0.002)
                            * 60
                            * r.devices.eta_discharge
                            / remaining,
                        )
                    elif abs(midpoint(b).p_mw[j]) > 1e-10:
                        # Cancel the lagged PCS midpoint with a bounded inverse
                        # command. The following zero command reaches the explicit
                        # settling band; full tube checks still approve both steps.
                        alpha = r.devices.resources[j].alpha_p
                        p[j] = -(1 - alpha) * midpoint(b).p_mw[j] / alpha
                    target = TubeCommand(p_mw=tuple(p), q_mvar=u.q_mvar)
                    choice = approach_device_command(
                        model_at(r, minute),
                        midpoint(b),
                        target,
                        movement_fraction=fraction,
                        next_model=model_at(r, minute + 1),
                    )
                    # The continuous certificate encloses P and Q in a box.
                    # A command on the wind Q/P cone can be valid at both
                    # endpoints yet fail that box while P is still rising.
                    # Stage Q behind the already attained active power.
                    q = list(choice.q_mvar)
                    for j, resource in enumerate(r.devices.resources):
                        if resource.q_over_p is not None:
                            cap = (
                                0.98
                                * resource.q_over_p
                                * max(0.0, min(b.p_mw[j].low, choice.p_mw[j]))
                            )
                            q[j] = min(cap, max(-cap, q[j]))
                    choice = choice.model_copy(update={"q_mvar": tuple(q)})
                choices.append(choice)
            try:
                step = certify_step(domain, boxes, tuple(choices), minute)
            except ValueError as exc:
                failure = str(exc)
            else:
                break
        else:
            raise ValueError(failure)
        commands.append(tuple(choices))
        steps.append(step)
        boxes = step.endpoints
    raise ValueError(
        "return did not reach its certified invariant set within the search budget"
    )
