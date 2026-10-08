"""Accept a bounded multi-region reserve event only with a certified successor."""

# pyright: strict
from oilfield_energy.modules.control.api import point_box, validate_device_box
from oilfield_energy.modules.control.contracts import TubeBox, TubeCommand, TubeState
from .contracts_reserve import ReserveActivationCommand
from .contracts_safety import ClusterSafetyDomain, ClusterForecast, ClusterReserveEvent
from .reserve_delivery import reserve_activation_command
from .safety_network import model_at, nodal_demands, certify_joint_boxes
from .safety_reserve import certify_state_reserve
from .safety_certificates import certify_continuation


def certify_event(
    domain: ClusterSafetyDomain,
    states: tuple[TubeState, ...],
    minute: int,
    terminal: tuple[TubeCommand, ...],
    digest: str,
    number: int,
) -> ClusterReserveEvent:
    boxes = tuple(point_box(s) for s in states)
    certificate = certify_state_reserve(domain, boxes, minute)
    if certificate is None or certificate.status != "certified":
        raise ValueError("reserve event requires a fresh full product certificate")
    assert domain.reserve_policy is not None and domain.delivery_policy is not None
    duration = (
        domain.reserve_policy.support_minutes + domain.delivery_policy.response_minutes
    )
    post: list[TubeBox] = []
    for r, b, c in zip(domain.regions, boxes, certificate.regions, strict=True):
        i = next(i for i, x in enumerate(r.devices.resources) if x.kind == "storage")
        p = list(b.p_mw)
        p[i] = c.storage_power_mw
        post.append(b.model_copy(update={"p_mw": tuple(p), "energy_mwh": c.energy_mwh}))
    enclosure = tuple(post)
    # A stronger sufficient admission rule: even the whole call envelope retains
    # full reserve adequacy. Thus no accepted original recovery deadline is lost.
    for k in range(minute, minute + duration + 1):
        for r, b in zip(domain.regions, enclosure, strict=True):
            validate_device_box(model_at(r, k), b)
        certify_joint_boxes(domain, enclosure, k)
        remaining = certify_state_reserve(domain, enclosure, k)
        if remaining is None or remaining.status != "certified":
            raise ValueError(
                "call envelope cannot preserve existing reserve commitments"
            )
    successor = certify_continuation(
        domain, enclosure, minute + duration, terminal, digest
    )
    return ClusterReserveEvent(
        number=number,
        start_minute=minute,
        end_minute=minute + duration,
        certificate=certificate,
        successor=successor,
    )


def activation_commands(
    domain: ClusterSafetyDomain,
    event: ClusterReserveEvent,
    states: tuple[TubeState, ...],
    observed: tuple[ClusterForecast, ...],
    minute: int,
    requests: tuple[float, ...],
    previous: tuple[ReserveActivationCommand, ...],
) -> tuple[tuple[TubeCommand, ...], tuple[ReserveActivationCommand, ...]]:
    if len(requests) != len(domain.regions):
        raise ValueError("reserve request region axis differs")
    commands: list[TubeCommand] = []
    activation: list[ReserveActivationCommand] = []
    for i, (r, s, f, c, request) in enumerate(
        zip(
            domain.regions,
            states,
            observed,
            event.certificate.regions,
            requests,
            strict=True,
        )
    ):
        p, q = nodal_demands(r, point_box(s), f, exclude_storage=True)
        j = next(j for j, x in enumerate(r.devices.resources) if x.kind == "storage")
        last = previous[i] if previous else None
        a = reserve_activation_command(
            c,
            demand_p_mw=tuple((x.low + x.high) / 2 for x in p),
            demand_q_mvar=tuple((x.low + x.high) / 2 for x in q),
            baseline_power_mw=0.0,
            previous_power_mw=s.p_mw[j],
            requested_pcc_up_mw=request,
            previous_request_mw=last.requested_pcc_up_mw if last else 0.0,
            elapsed_minutes=minute - event.start_minute,
            previous_command=last,
        )
        active = list(s.p_mw)
        active[j] = a.command_storage_mw
        commands.append(TubeCommand(p_mw=tuple(active), q_mvar=s.q_mvar))
        activation.append(a)
    return tuple(commands), tuple(activation)
