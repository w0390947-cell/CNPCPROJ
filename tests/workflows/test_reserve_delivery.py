"""All-call certificate versus independently integrated alternating activations."""

from math import exp

import pytest

from oilfield_energy.modules.dispatch.contracts import ReserveDeliveryPolicy
from oilfield_energy.modules.power_flow.contracts import (
    EnvelopeBranch,
    PowerInterval,
    RadialEnvelope,
)
from oilfield_energy.workflows.cluster_execution.api import (
    certify_reserve_service,
    certify_cluster_reserve,
    reserve_activation_command,
)
from oilfield_energy.workflows.cluster_execution.contracts import ReserveServiceInput


def service(**changes):
    zero = PowerInterval(low=0, high=0)
    network = RadialEnvelope(
        network_id="synthetic",
        buses=("pcc", "load"),
        branches=(
            EnvelopeBranch(
                parent=0, child=1, r_pu=0.001, x_pu=0.001, tap=1, rating_mva=10
            ),
        ),
        slack=0,
        base_mva=10,
        slack_voltage_pu=1,
        shunt_q_mvar=(0, 0),
        voltage_min_pu=0.95,
        voltage_max_pu=1.05,
        pcc_min_mw=0,
        pcc_max_mw=10,
        pf_min=0.9,
    )
    return ReserveServiceInput(
        **(
            dict(
                region="A",
                start_minute=0,
                support_minutes=15,
                policy=ReserveDeliveryPolicy(),
                network=network,
                demand_p_mw=(zero, PowerInterval(low=2, high=2)),
                demand_q_mvar=(zero, zero),
                storage_bus=1,
                baseline_power_mw=zero,
                initial_power_mw=0,
                initial_energy_mwh=2,
                minimum_energy_mwh=0.5,
                maximum_energy_mwh=4.5,
                maximum_power_mw=2,
                storage_s_mva=2.2,
                storage_q_mvar=zero,
                eta_charge=0.95,
                eta_discharge=0.95,
                response_alpha=1 - exp(-0.5),
                ramp_mw_per_minute=0.65,
                required_mw=0.25,
                activation_headroom_mw=0.3,
                context_id="synthetic-A",
            )
            | changes
        )
    )


def test_certificate_covers_causal_sign_changes_and_whole_energy_integral():
    result = certify_reserve_service(service())
    assert result.status == "certified", result.reason
    previous = request = 0.0
    energy = 2.0
    previous_command = None
    for k in range(16):
        requested = (0, 0.25, 0, -0.25)[k % 4]
        command = reserve_activation_command(
            result,
            demand_p_mw=(0, 2),
            demand_q_mvar=(0, 0),
            baseline_power_mw=0,
            previous_power_mw=previous,
            requested_pcc_up_mw=requested,
            previous_request_mw=request,
            elapsed_minutes=k,
            previous_command=previous_command,
        )
        actual = previous + result.inputs.response_alpha * (
            command.command_storage_mw - previous
        )
        assert actual == pytest.approx(command.target_storage_mw, abs=1e-12)
        # Linear intersample extension: independent quadrature includes zero crossing.
        for i in range(100):
            p = previous + (actual - previous) * (i + 0.5) / 100
            energy -= (p / 0.95 if p >= 0 else p * 0.95) / 6000
            assert result.energy_mwh.contains(energy)
        previous, request = actual, requested
        previous_command = command


@pytest.mark.parametrize(
    "changes,reason",
    [
        ({"ramp_mw_per_minute": 0.1}, "ramp"),
        ({"initial_energy_mwh": 0.52}, "energy"),
        ({"storage_q_mvar": PowerInterval(low=2.19, high=2.19)}, "P/Q"),
    ],
)
def test_static_energy_or_headroom_does_not_replace_deliverability(changes, reason):
    result = certify_reserve_service(service(**changes))
    assert result.status == "unknown" and reason in result.reason


def test_thermal_constraint_and_simultaneous_cluster_calls_are_enforced():
    base = service()
    line = base.network.branches[0].model_copy(update={"rating_mva": 2.1})
    blocked = certify_reserve_service(
        base.model_copy(
            update={"network": base.network.model_copy(update={"branches": (line,)})}
        )
    )
    assert blocked.status == "unknown" and not blocked.network.certified
    rows = tuple(
        certify_reserve_service(service(region=n, context_id=n))
        for n in ("A", "B", "C")
    )
    assert all(r.status == "certified" for r in rows)
    assert (
        certify_cluster_reserve(rows, import_limit_mw=6.5, pf_target=0.95).status
        == "unknown"
    )
    assert (
        certify_cluster_reserve(rows, import_limit_mw=8, pf_target=0.95).status
        == "certified"
    )


def test_joint_pf_and_stale_or_out_of_domain_calls_are_rejected():
    q = (PowerInterval(low=0, high=0), PowerInterval(low=0.7, high=0.7))
    rows = tuple(
        certify_reserve_service(service(region=n, context_id=n, demand_q_mvar=q))
        for n in ("A", "B", "C")
    )
    assert all(r.status == "certified" for r in rows)
    result = certify_cluster_reserve(rows, import_limit_mw=8, pf_target=0.95)
    assert result.status == "unknown" and "power factor" in result.reason
    row = certify_reserve_service(service())
    args = dict(
        demand_p_mw=(0, 2),
        demand_q_mvar=(0, 0),
        baseline_power_mw=0,
        previous_power_mw=0,
        requested_pcc_up_mw=0.25,
        previous_request_mw=0,
        elapsed_minutes=0,
    )
    for change in (
        {"elapsed_minutes": 16},
        {"previous_request_mw": -0.25},
        {"demand_p_mw": (0, 3)},
    ):
        with pytest.raises(ValueError):
            reserve_activation_command(row, **(args | change))


def test_declared_slew_is_used_instead_of_requiring_instant_full_reversal():
    result = certify_reserve_service(
        service(required_mw=0.4, activation_headroom_mw=0.42)
    )
    assert 2 * result.inputs.activation_headroom_mw > result.inputs.ramp_mw_per_minute
    assert result.status == "certified", result.reason
    assert result.maximum_change_mw < result.inputs.ramp_mw_per_minute
