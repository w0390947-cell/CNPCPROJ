"""Analytic transitions distinguish joint motion from fixed-P inner envelopes."""

from math import exp, nextafter, inf

import pytest

from oilfield_energy.modules.control.contracts import (
    TubeCommand,
    TubeDeviceModel,
    TubeResource,
    TubeState,
)
from oilfield_energy.kernel.intervals import Interval as I
from oilfield_energy.modules.dispatch.api import (
    pq_witness_violation,
    plan_reactive_envelope,
)
from oilfield_energy.modules.dispatch.contracts import (
    PQPlanningInput,
    PQPlanningWitness,
    ReactivePlanningPolicy,
    ReactivePlanningResource,
)


def example():
    alpha = 1 - exp(-0.5)
    model = TubeDeviceModel(
        resources=(
            TubeResource(
                resource_id="w",
                bus="b",
                kind="wind",
                p=I(low=0, high=2),
                q=I(low=-0.66, high=0.66),
                s_mva=2.2,
                q_over_p=0.3,
                alpha_p=alpha,
                ramp_p_mw=0.325,
            ),
            TubeResource(
                resource_id="s",
                bus="b",
                kind="storage",
                p=I(low=-0.5, high=0.5),
                q=I(low=0, high=0),
                s_mva=1,
                alpha_p=1,
                ramp_p_mw=0.325,
            ),
        ),
        alpha_q=alpha,
        aggregate_q_ramp_mvar=0.9,
        step_minutes=1,
        energy_mwh=I(low=0.2, high=2),
        eta_charge=0.95,
        eta_discharge=0.95,
        context_id="analytic",
    )
    inputs = PQPlanningInput(
        devices=model,
        initial=TubeState(p_mw=(2, 0), q_mvar=(0.552596, 0), energy_mwh=1),
        planning_steps=1,
        planning_minutes=15,
        transition_minutes=5,
        p_ceiling_mw=((2, 0.5),) + ((1.811699, 0.5),) * 15,
        polygon_sides=16,
        reserve_fraction=0.1,
    )
    first = TubeCommand(p_mw=(1.52, 0), q_mvar=(0.36, 0))
    end = TubeCommand(
        p_mw=((1 - alpha) * 2 + alpha * 1.52, 0),
        q_mvar=((1 - alpha) * 0.552596 + alpha * 0.36, 0),
    )
    return PQPlanningWitness(
        inputs=inputs, commands=((first, end, end, end, end),), endpoints=(end,)
    )


def test_joint_drop_passes_while_fixed_plan_p_excludes_the_same_endpoint():
    w = example()
    end = w.endpoints[0]
    assert pq_witness_violation(w) < 1e-12
    rows = plan_reactive_envelope(
        (ReactivePlanningResource("w", ((-0.3, 1, 0), (-0.3, -1, 0)), 0.552596, True),),
        steps=1,
        step_minutes=15,
        time_constant_minutes=2,
        ramp_mvar_per_minute=0.9,
        policy=ReactivePlanningPolicy(),
    )
    assert (
        max(
            row.p_coefficient * end.p_mw[0]
            + row.q_coefficient * end.q_mvar[0]
            - row.upper
            for row in rows
        )
        > 0.009
    )
    # Independent real recurrence and engineering limits, not row self-comparison.
    p, q = 2.0, 0.552596
    for k, u in enumerate(w.commands[0]):
        oldp, oldq = p, q
        p += (1 - exp(-0.5)) * (u.p_mw[0] - p)
        q += (1 - exp(-0.5)) * (u.q_mvar[0] - q)
        assert abs(q) <= 0.3 * p and p <= w.inputs.p_ceiling_mw[k + 1][0]
        assert abs(p - oldp) <= 0.325 and abs(q - oldq) <= 0.9


def test_missing_active_axis_and_invalid_actual_q_are_rejected():
    w = example()
    raw = w.inputs.model_dump(mode="json")
    raw["initial"]["p_mw"] = []
    with pytest.raises(ValueError):
        PQPlanningInput.model_validate(raw)
    raw = w.inputs.model_dump(mode="json")
    raw["initial"]["q_mvar"][0] = 0.61
    with pytest.raises(ValueError):
        PQPlanningInput.model_validate(raw)


def test_future_ceiling_and_uncoordinated_q_drop_are_not_relaxed():
    w = example()
    bad = w.model_copy(
        update={
            "inputs": w.inputs.model_copy(
                update={"p_ceiling_mw": ((2, 0.5),) + ((1.7, 0.5),) * 15}
            )
        }
    )
    assert pq_witness_violation(bad) > 0.1
    first = w.commands[0][0].model_copy(update={"q_mvar": (0.552596, 0)})
    bad = w.model_copy(update={"commands": ((first, *w.commands[0][1:]),)})
    assert pq_witness_violation(bad) > 0.09


def test_storage_cannot_change_power_during_a_claimed_interval_hold():
    w = example()
    commands = list(w.commands[0])
    commands[0] = commands[0].model_copy(update={"p_mw": (1.52, 0.1)})
    bad = w.model_copy(update={"commands": (tuple(commands),)})
    assert pq_witness_violation(bad) >= 0.1


def test_joint_command_capability_and_endpoint_tampering_are_detected():
    w = example()
    commands = list(w.commands[0])
    commands[0] = commands[0].model_copy(update={"q_mvar": (0.6, 0)})
    assert (
        pq_witness_violation(w.model_copy(update={"commands": (tuple(commands),)}))
        > 0.1
    )


def test_opposing_q_actions_consume_absolute_shared_budget():
    w = example()
    resources = list(w.inputs.devices.resources)
    resources[1] = resources[1].model_copy(update={"q": I(low=-0.66, high=0.66)})
    model = w.inputs.devices.model_copy(
        update={"resources": tuple(resources), "aggregate_q_ramp_mvar": 0.1}
    )
    commands = list(w.commands[0])
    commands[0] = commands[0].model_copy(update={"q_mvar": (0.36, 0.36)})
    bad = w.model_copy(
        update={
            "inputs": w.inputs.model_copy(update={"devices": model}),
            "commands": (tuple(commands),),
        }
    )
    alpha = 1 - exp(-0.5)
    changes = (alpha * (0.36 - 0.552596), alpha * 0.36)
    assert abs(sum(changes)) < 0.1 < sum(abs(v) for v in changes)
    assert pq_witness_violation(bad) > 0.1


def test_unmatched_response_keeps_a_valid_rectangular_transition():
    w = example()
    resources = list(w.inputs.devices.resources)
    resources[0] = resources[0].model_copy(update={"alpha_p": 1.0})
    model = w.inputs.devices.model_copy(update={"resources": tuple(resources)})
    inputs = w.inputs.model_copy(
        update={"devices": model, "p_ceiling_mw": ((2, 0.5),) * 16}
    )
    first = TubeCommand(p_mw=(1.85, 0), q_mvar=(0.36, 0))
    end = w.endpoints[0].model_copy(update={"p_mw": (1.85, 0)})
    witness = PQPlanningWitness(
        inputs=inputs, commands=((first, end, end, end, end),), endpoints=(end,)
    )
    assert pq_witness_violation(witness) < 1e-12
    assert 0.552596 < 0.3 * 1.85  # Worst mixed capability corner.


def test_svg_at_legal_asymmetric_boundary_is_not_shrunk_by_a_disk_polygon():
    w = example()
    svg = TubeResource(
        resource_id="svg",
        bus="b",
        kind="svg",
        p=I(low=0, high=0),
        q=I(low=-0.1, high=2),
        s_mva=2,
        alpha_p=1,
        ramp_p_mw=0,
    )
    model = w.inputs.devices.model_copy(
        update={"resources": (*w.inputs.devices.resources, svg)}
    )
    inputs = w.inputs.model_copy(
        update={
            "devices": model,
            "reserve_fraction": 0,
            "initial": TubeState(p_mw=(2, 0, 0), q_mvar=(0.552596, 0, 2), energy_mwh=1),
            "p_ceiling_mw": tuple((*v, 0) for v in w.inputs.p_ceiling_mw),
        }
    )

    def extend(c):
        return TubeCommand(p_mw=(*c.p_mw, 0), q_mvar=(*c.q_mvar, 2))

    witness = PQPlanningWitness(
        inputs=inputs,
        commands=(tuple(extend(c) for c in w.commands[0]),),
        endpoints=(extend(w.endpoints[0]),),
    )
    assert pq_witness_violation(witness) < 1e-12
    assert (
        pq_witness_violation(
            w.model_copy(
                update={
                    "endpoints": (w.endpoints[0].model_copy(update={"p_mw": (1.7, 0)}),)
                }
            )
        )
        > 0.1
    )


def zero_landing(*, initial_p=0.0006, kind="pv", ratio=None, initial_q=0.0):
    w = example()
    r = w.inputs.devices.resources[0].model_copy(
        update={"kind": kind, "alpha_p": 0.5, "q_over_p": ratio, "ramp_p_mw": 0.001}
    )
    model = w.inputs.devices.model_copy(
        update={"resources": (r, w.inputs.devices.resources[1]), "alpha_q": 0.5}
    )
    inputs = PQPlanningInput(
        devices=model,
        initial=TubeState(p_mw=(initial_p, 0), q_mvar=(initial_q, 0), energy_mwh=1),
        planning_steps=1,
        planning_minutes=5,
        transition_minutes=3,
        p_ceiling_mw=((2, 0.5),) * 4 + ((0, 0.5),) * 2,
        polygon_sides=16,
        reserve_fraction=0,
    )
    u = TubeCommand(p_mw=(0, 0), q_mvar=(0 if ratio is not None else initial_q, 0))
    end = TubeCommand(p_mw=(0, 0), q_mvar=u.q_mvar)
    return PQPlanningWitness(inputs=inputs, commands=((u, u, u),), endpoints=(end,))


def test_zero_ceiling_landing_matches_finite_resolution_execution():
    from oilfield_energy.modules.control.api import execute_tube_command

    w = zero_landing()
    assert pq_witness_violation(w) < 1e-12
    actual = w.inputs.initial
    for u in w.commands[0]:
        actual = execute_tube_command(w.inputs.devices, actual, u)
    assert actual.p_mw == w.endpoints[0].p_mw == (0, 0)
    # Pure exponential decay cannot equal zero at any finite step.
    assert 0.5**3 * w.inputs.initial.p_mw[0] > 0


def test_zero_landing_preserves_wind_ratio_and_independent_pv_q():
    wind = zero_landing(kind="wind", ratio=0.3, initial_q=0.00012)
    assert pq_witness_violation(wind) < 1e-12
    pv = zero_landing(initial_q=0.01)
    assert pq_witness_violation(pv) < 1e-12
    assert pv.endpoints[0].q_mvar[0] == 0.01


def test_zero_landing_cannot_skip_response_or_ramp_or_shared_q_limits():
    w = zero_landing(initial_p=0.002)
    assert pq_witness_violation(w) > 0.0001
    w = zero_landing()
    resources = list(w.inputs.devices.resources)
    resources[0] = resources[0].model_copy(update={"ramp_p_mw": 0.0001})
    inputs = w.inputs.model_copy(
        update={
            "devices": w.inputs.devices.model_copy(
                update={"resources": tuple(resources)}
            )
        }
    )
    assert pq_witness_violation(w.model_copy(update={"inputs": inputs})) > 0.0001
    w = zero_landing(kind="wind", ratio=0.3, initial_q=0.00012)
    inputs = w.inputs.model_copy(
        update={
            "devices": w.inputs.devices.model_copy(
                update={"aggregate_q_ramp_mvar": 0.00001}
            )
        }
    )
    assert pq_witness_violation(w.model_copy(update={"inputs": inputs})) > 0.00004


def test_zero_landing_threshold_uses_exact_device_condition():
    w = zero_landing(initial_p=0.0002)

    def first_minute(w, p):
        return PQPlanningWitness(
            inputs=w.inputs.model_copy(
                update={
                    "initial": w.inputs.initial.model_copy(update={"p_mw": (p, 0)}),
                    "transition_minutes": 1,
                    "settling_utilization": 1,
                    "p_ceiling_mw": ((2, 0.5),) + ((0, 0.5),) * 5,
                }
            ),
            commands=((w.commands[0][0],),),
            endpoints=w.endpoints,
        )

    assert pq_witness_violation(first_minute(w, 0.0002)) < 1e-12
    assert pq_witness_violation(first_minute(w, nextafter(0.0002, inf))) > 0.00009
    # A positive future ceiling does not enable the forced-zero construction.
    positive = first_minute(w, 0.0002)
    positive = positive.model_copy(
        update={
            "inputs": positive.inputs.model_copy(
                update={"p_ceiling_mw": ((2, 0.5),) * 6}
            )
        }
    )
    assert pq_witness_violation(positive) > 0.00009
