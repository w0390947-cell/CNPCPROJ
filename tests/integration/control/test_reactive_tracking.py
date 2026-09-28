"""Real AC prediction, one physical response budget, and arbitration rejection."""

import numpy as np
import pytest

from oilfield_energy.ac_power_flow import backward_forward_sweep_resolved
from oilfield_energy.network_model import (
    NetworkBranchKind, NetworkBus, NetworkDataProvenance, NetworkModelV2,
    NetworkOperatingMode, SeriesBranch, assess_network_model,
)
from oilfield_energy.network_scenarios import NetworkSecurityLimits
from oilfield_energy.reactive_correction import correct_reactive_dispatch
from oilfield_energy.reactive_execution import ReactiveCapability


@pytest.mark.parametrize("blocked", [False, True])
def test_actual_ac_tracking_improves_only_through_accepted_device_commands(blocked):
    model = NetworkModelV2('q-test', 10., 'A', (NetworkBus('A','A',10.), NetworkBus('B','B',10.)),
        (SeriesBranch('AB','A','B',NetworkBranchKind.LINE,.001,.005,4.),),
        (NetworkOperatingMode('normal','q-test'),), 'normal', NetworkDataProvenance('test','1',synthetic=True))
    network = assess_network_model(model).require_current_solver_ready()
    limits = NetworkSecurityLimits(.95,1.05,.15,4.,.9)
    args = dict(p_demand_mw=np.array([0.,2.]), q_demand_before_mvar=np.array([0.,.5]),
        resource_buses=['B','B'], current_q=np.zeros(2), preferred_targets=np.array([0.,.3]),
        capabilities=(ReactiveCapability('disabled',0.,0.), ReactiveCapability('svg',-1.2,1.2)),
        project_targets=(lambda x: np.zeros(2)) if blocked else (lambda x:x),
        slack_voltage_pu=1., alpha=.4, ramp_mvar=.9)
    original = correct_reactive_dispatch(network, limits, **args)
    revised = correct_reactive_dispatch(network, limits, **args, pcc_target_mvar=.2)
    def actual(result):
        return backward_forward_sweep_resolved(network,args['p_demand_mw'],
            args['q_demand_before_mvar']-np.array([0.,sum(result.predicted_actual)]))
    before, after = actual(original), actual(revised)
    assert after['converged'] and min(after['voltage_pu']) >= .95
    assert max(after['line_loading_pu']) <= 1
    assert revised.predicted_actual[0] == 0
    np.testing.assert_allclose(revised.predicted_actual, .4 * revised.targets)
    assert sum(abs(revised.predicted_actual)) <= .9
    assert revised.tracking is not None
    if blocked:
        assert abs(after['pcc_q_mvar']-.2) > .05
        assert np.max(np.abs(revised.targets)) == 0
        assert revised.tracking.status == 'limited'
    else:
        assert abs(before['pcc_q_mvar']-.2) > .05
        assert abs(after['pcc_q_mvar']-.2) < 1e-4
        assert revised.tracking.status == 'improved'
    np.testing.assert_array_equal(args['current_q'], np.zeros(2))
