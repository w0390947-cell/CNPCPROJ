from __future__ import annotations

import sys
import unittest
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from oilfield_energy.ac_consistency import solve_case_ac_consistent
from oilfield_energy.ac_power_flow import backward_forward_sweep_resolved
from oilfield_energy.data import build_synthetic_case
from oilfield_energy.device_control import simulate_device_tracking
from oilfield_energy.modules.control.contracts import BusSeries, PlantInputs
from oilfield_energy.network_model import (
    NetworkBus, SeriesBranch, NetworkBranchKind, NetworkModelV2,
    NetworkOperatingMode, NetworkDataProvenance, assess_network_model,
)
from oilfield_energy.network_scenarios import NetworkSecurityLimits
from oilfield_energy.reactive_correction import correct_reactive_dispatch
from oilfield_energy.reactive_execution import ReactiveCapability, calculate_reactive_capabilities
from oilfield_energy.resource_control_contracts import ResourceSchedule, ResourceType


class CorrectiveExecutionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        c=build_synthetic_case(steps=4)
        cls.case=replace(c,time_hours=np.arange(4)*.25,assumptions=replace(c.assumptions,dt_hours=.25))
        cls.mg=cls.case.microgrids[0]
        checked=solve_case_ac_consistent(cls.case,[cls.mg.name],time_limit_seconds=60)
        assert checked.passed
        cls.solved=checked.optimization

    def plant(self, p=1., q=1., mg=None):
        mg=mg or self.mg
        def group(values, factor=1.):
            return tuple(BusSeries(bus_id=b,values=tuple(float(v) for v in np.repeat(a,15)*factor)) for b,a in values.items())
        return PlantInputs(start=datetime(2026,9,1,tzinfo=timezone.utc),step_minutes=1,
            load_p=group(mg.load_p_mw,p),load_q=group(mg.load_q_mvar,q),
            wind_available=group(mg.wind_available_mw),pv_available=group(mg.pv_available_mw))

    def run_case(self, **kwargs):
        return simulate_device_tracking(self.case,self.mg,self.solved,
                                        reverse_flow_probability_15=np.zeros(4),**kwargs)

    def assert_execution_envelopes(self, result, mg=None):
        mg=mg or self.mg
        for record in result.network_feedback:
            if record['stage']!='post_control': continue
            control=record['reactive_control']
            movement=np.sum(np.abs(np.array(control['q_executed_mvar'])-control['q_before_mvar']))
            self.assertLessEqual(movement,control['aggregate_q_ramp_mvar']+1e-8)
            for device in record['inputs']['snapshot']['devices']:
                p,q,b,kind=device['p_mw'],device['q_mvar'],device['bus_id'],device['resource_type']
                if kind=='svg':
                    self.assertGreaterEqual(q,mg.svg_q_min_mvar-1e-8)
                    self.assertLessEqual(q,mg.svg_q_max_mvar+1e-8)
                else:
                    capacity=mg.wind_capacity_mva[b] if kind=='wind' else mg.pv_capacity_mva[b] if kind=='pv' else mg.storage.s_max_mva
                    self.assertLessEqual(np.hypot(p,q),capacity+1e-7)
                if kind=='wind': self.assertLessEqual(abs(q),.3*p+1e-7)
                if kind=='pv' and not mg.pv_can_control_reactive(b): self.assertEqual(q,0.)
                if kind=='storage' and not mg.storage_reactive_enabled: self.assertEqual(q,0.)

    def test_voltage_transient_is_retained_when_power_response_is_bounded(self):
        r=self.run_case(slack_voltage_pu=.951)
        finals=[x for x in r.network_feedback if x['stage']=='post_control']
        # This exact marginal fixture formerly passed by spending two storage
        # ramp budgets in one minute. Keep the fixture and voltage floor: a
        # bounded best-effort response must retain its observed violation.
        failed = [x for x in finals if not x["voltage_within_limits"]]
        self.assertEqual([x["time_minutes"] for x in failed], [45.0])
        for record in failed:
            dynamics = r.storage_dynamics[int(record["time_minutes"])]
            self.assertFalse(dynamics.physical_override or dynamics.hard_override)
            self.assertTrue(dynamics.ramp_compliant)
            self.assertAlmostEqual(abs(dynamics.actual_mw - dynamics.previous_mw), 0.65)
            self.assertGreater(dynamics.ordinary_unserved_mw, 0)
            control = record["reactive_control"]
            self.assertEqual(control["status"], "best_effort")
            self.assertFalse(control["predicted_safe"])
            self.assertFalse(control["post_action_safe"])
            movement = np.sum(
                np.abs(np.array(control["q_executed_mvar"]) - control["q_before_mvar"])
            )
            self.assertAlmostEqual(movement, 0.9)
        self.assertTrue(all(x["voltage_within_limits"] for x in finals[46:]))
        # Successful corrections elsewhere remain covered, including preserved
        # pre-action voltage evidence; no global infeasibility claim is made.
        corrected = {x["time_minutes"] for x in finals if x["voltage_within_limits"]}
        self.assertTrue(
            any(
                x["stage"] == "pre_reactive"
                and not x["voltage_within_limits"]
                and x["time_minutes"] in corrected
                for x in r.network_feedback
            )
        )
        self.assertTrue(all(x['line_capacity_within_limits'] for x in finals))
        self.assertGreaterEqual(np.min(r.actual_power_factor),.9-1e-8)
        self.assertGreaterEqual(np.min(r.pcc_actual_mw),.15-1e-8)
        self.assertGreater(r.network_violation_steps,0)
        self.assertFalse(r.network_security_passed)  # Already-observed violations are history.
        self.assert_execution_envelopes(r)

    def test_all_resources_recover_pf_after_ramp_limited_transient(self):
        r=self.run_case(plant_inputs=self.plant(q=2.))
        self.assertTrue(np.all(r.shancheng_reactive_command_accepted))
        self.assertTrue(np.all(r.reactive_execution_known))
        self.assertGreater(r.pf_violations_after_safety,0)  # No imaginary instantaneous jump.
        self.assertLessEqual(r.pf_violations_after_safety,2)
        self.assertTrue(np.all(r.actual_power_factor[2:]>=.9-1e-8))
        self.assertTrue(np.all(r.reactive_dispatch_unserved_mvar[2:]<=1e-8))
        first=next(x for x in r.network_feedback if x['stage']=='post_control')
        self.assertEqual(first['reactive_control']['status'],'best_effort')
        self.assertFalse(first['reactive_control']['post_action_safe'])
        for resource_id,actual in r.reactive_resource_actual_mvar.items():
            if ':pv:' in resource_id or ':storage:' in resource_id:
                self.assertGreater(float(np.max(actual-r.reactive_resource_plan_mvar[resource_id])),.1)
        self.assert_execution_envelopes(r)

    def test_pf_boundary_uses_post_action_ac_losses(self):
        r=self.run_case(plant_inputs=self.plant(p=.5))
        self.assertEqual(r.pf_violations_after_safety,0)
        self.assertEqual(r.no_reverse_violations_after_safety,0)
        self.assertTrue(np.all(r.shancheng_reactive_command_accepted))
        self.assertGreaterEqual(np.min(r.actual_power_factor),.9)
        self.assertLessEqual(np.max(r.reactive_dispatch_unserved_mvar),1e-8)
        self.assert_execution_envelopes(r)

    def test_disabled_resources_are_never_used_to_fake_pf_recovery(self):
        mg=replace(self.mg,pv_reactive_enabled={b:False for b in self.mg.pv_available_mw},
                   storage_reactive_enabled=False,wind_q_abs_over_p_max={b:.3 for b in self.mg.wind_available_mw})
        c=replace(self.case,microgrids=[mg])
        plan=solve_case_ac_consistent(c,[mg.name],time_limit_seconds=60)
        self.assertTrue(plan.passed)
        r=simulate_device_tracking(c,mg,plan.optimization,plant_inputs=self.plant(q=2.,mg=mg),reverse_flow_probability_15=np.zeros(4))
        self.assertGreater(r.pf_violations_after_safety,0)
        self.assertGreater(np.max(r.reactive_dispatch_unserved_mvar),0)
        self.assert_execution_envelopes(r,mg)


class CorrectiveBoundsTests(unittest.TestCase):
    def setUp(self):
        model=NetworkModelV2('test',10.,'A',(NetworkBus('A','A',10.),NetworkBus('B','B',10.)),
            (SeriesBranch('AB','A','B',NetworkBranchKind.LINE,.001,.005,4.),),
            (NetworkOperatingMode('normal','test'),),'normal',NetworkDataProvenance('test','1',synthetic=True))
        self.network=assess_network_model(model).require_current_solver_ready()
        self.limits=NetworkSecurityLimits(.95,1.05,.15,4.,.9)
        self.args=dict(p_demand_mw=np.array([0.,1.]),q_demand_before_mvar=np.array([0.,-1.]),
            resource_buses=['B','B'],current_q=np.zeros(2),preferred_targets=np.zeros(2),
            capabilities=(ReactiveCapability('disabled',0.,0.),ReactiveCapability('svg',-.8,1.2)),
            project_targets=lambda x:x,slack_voltage_pu=1.,alpha=1.,ramp_mvar=.9)

    def test_signed_pf_and_asymmetric_device_interval(self):
        r=correct_reactive_dispatch(self.network,self.limits,**self.args)
        self.assertEqual(r.status,'corrected')
        self.assertEqual(r.targets[0],0.)
        self.assertGreaterEqual(r.targets[1],-.8)
        q=self.args['q_demand_before_mvar']-np.array([0.,np.sum(r.predicted_actual)])
        f=backward_forward_sweep_resolved(self.network,self.args['p_demand_mw'],q)
        self.assertGreaterEqual(abs(f['pcc_p_mw'])/np.hypot(f['pcc_p_mw'],f['pcc_q_mvar']),.9)
        self.assertGreaterEqual(f['pcc_p_mw'],.15)

    def test_no_response_budget_and_nonconvergence_are_not_safe(self):
        r=correct_reactive_dispatch(self.network,self.limits,**dict(self.args,ramp_mvar=0.))
        self.assertEqual(r.status,'held')
        np.testing.assert_array_equal(r.predicted_actual,np.zeros(2))
        r=correct_reactive_dispatch(self.network,replace(self.limits,max_iterations=1),**self.args)
        self.assertEqual(r.status,'unavailable')

    def test_rejected_group_residual_is_reallocated_to_other_resources(self):
        args=dict(self.args,capabilities=(ReactiveCapability('blocked',-.8,1.2),
                                         ReactiveCapability('available',-.8,1.2)),
                  preferred_targets=np.array([-.6,0.]),
                  project_targets=lambda x:np.array([0.,x[1]]))
        r=correct_reactive_dispatch(self.network,self.limits,**args)
        self.assertIn(r.status,('safe','corrected'))
        self.assertAlmostEqual(np.sum(r.controls),np.sum(r.targets),places=8)
        self.assertAlmostEqual(r.controls[0],0.,places=8)
        self.assertLess(r.targets[1],-.5)

    def test_fixed_slack_and_active_overload_have_explicit_failures(self):
        r=correct_reactive_dispatch(self.network,self.limits,**dict(self.args,slack_voltage_pu=.85))
        self.assertEqual(r.status,'fixed_slack_infeasible')
        for p in (8.,-8.):
            r=correct_reactive_dispatch(self.network,self.limits,**dict(self.args,p_demand_mw=np.array([0.,p])))
            self.assertEqual(r.status,'fixed_active_infeasible')

    def test_nonfinite_optimizer_result_holds_without_emitting_invalid_targets(self):
        with patch('oilfield_energy.reactive_correction.minimize',
                   return_value=SimpleNamespace(x=np.array([np.nan]))):
            r=correct_reactive_dispatch(self.network,self.limits,**self.args)
        self.assertEqual(r.status,'held')
        np.testing.assert_array_equal(r.targets,np.zeros(2))

    def test_wind_ratio_and_mva_circle_both_apply(self):
        mg=build_synthetic_case(steps=1).microgrids[0]
        bus=next(iter(mg.wind_available_mw))
        mg=replace(mg,wind_q_abs_over_p_max={bus:.3},wind_capacity_mva={bus:10.})
        schedule=ResourceSchedule('wind',bus,ResourceType.WIND,np.array([9.9]),np.zeros(1))
        cap=calculate_reactive_capabilities(mg,[schedule],{'wind':9.9})[0]
        self.assertAlmostEqual(cap.maximum_mvar,np.sqrt(100-9.9**2))


if __name__=='__main__': unittest.main()
