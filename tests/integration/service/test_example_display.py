"""Real controller commits are observed, never reconstructed by the display feed."""
from types import SimpleNamespace
import pytest
from oilfield_energy.live_journal import LiveJournal, read_live
from oilfield_energy.bootstrap.adapters.cluster_rolling import RollingClusterEngine
from oilfield_energy.bootstrap.adapters.cluster_safety import ClusterSafetyPort
from oilfield_energy.bootstrap.adapters.cluster_execution import execution_inputs
from oilfield_energy.workflows.cluster_execution.contracts import ExecutionPolicy, RollingUpdate
from oilfield_energy.bootstrap.adapters.cluster_live import ClusterLiveEvent
from tests.support.cluster_safety import safety_domain
from tests.support.reserve_case import stationary_reserve_case


@pytest.mark.parametrize('stop', [False, True])
def test_cluster_live_is_emitted_after_real_joint_commits(tmp_path, stop):
    case = stationary_reserve_case()
    domain = safety_domain()
    _, plants, _ = execution_inputs(case, packaged=False, events=[])
    policy = ExecutionPolicy(update_minutes=1, horizon_minutes=2)
    port = ClusterSafetyPort(case, policy, True, plants, domain=domain)
    engine = RollingClusterEngine.__new__(RollingClusterEngine)
    engine.safety = port
    engine.safety_domain = domain
    engine.safety_failure = ''
    engine.names = tuple(r.region for r in domain.regions)
    engine.total = 2
    engine.policy = policy
    engine.progress = lambda _: None
    engine.rolling_progress = None
    engine.live = LiveJournal(tmp_path).publish
    engine._sync_safety()
    engine.prepare = lambda start, end, horizon: RollingUpdate(
        start_minute=start, end_minute=end, horizon_end_minute=horizon,
        status='unknown', continuation_available=True, reason='economic solver unavailable',
    )
    advance = port.advance
    def commit(reason):
        if stop and port.controller.minute == 1:
            raise ValueError('test stop before next commit')
        advance(reason)
    port.advance = commit
    engine.run()
    events = read_live(tmp_path)['events']
    for event in events:
        ClusterLiveEvent.model_validate(event)
    assert events[0]['kind'] == 'cluster_initial'
    assert 'status' not in events[0]  # No fabricated overall success.
    cycles = [e for e in events if e['kind'] == 'cluster_cycle']
    assert len(cycles) == (1 if stop else 2)
    assert [e['minute'] for e in cycles] == list(range(1, len(cycles) + 1))
    assert [e['step'] for e in cycles] == [s.model_dump(mode='json') for s in engine.safety_evidence().steps]
    for i, event in enumerate(cycles):
        for region in engine.names:
            assert event['pcc'][region] == list(port.outputs[region][i])
    for region in engine.names:
        archived = port.minute_evidence(region, engine.total)
        assert list(archived.p_target_samples_mw) == [e['pcc'][region][2] for e in cycles]
        assert list(archived.q_target_samples_mvar) == [e['pcc'][region][3] for e in cycles]
    windows = [e['window'] for e in events if e['kind'] == 'cluster_window']
    assert windows[0]['actual_end_energy_mwh'] == {}
    assert windows[-1] == engine.updates[-1].model_dump(mode='json')
    if not stop:
        name = engine.names[0]
        p, q, _, _ = port.outputs[name][1]
        port.outputs[name][1] = (p, q, 1.23, 0.1)
        archived = port.minute_evidence(name, engine.total)
        assert archived.p_target_samples_mw == (None, 1.23)
        assert archived.q_target_samples_mvar == (None, 0.1)
        assert archived.p_target_mw == ()  # Preserve the old complete-series contract.


def test_single_preview_uses_the_same_domain_as_formal_execution(monkeypatch):
    from oilfield_energy.bootstrap.adapters.single_example import describe_single_example
    from oilfield_energy.bootstrap.adapters import single_unified
    from oilfield_energy.bootstrap.adapters.project_dataset import build_synthetic_case
    from oilfield_energy.bootstrap.adapters.single_planning import single_execution_policy
    preview = describe_single_example()
    captured = {}
    class ReachedExecution(Exception):
        pass
    def enter(domain, initial, **kwargs):
        captured.update(domain=domain, initial=initial, total=kwargs['total_steps'])
        kwargs['live']({'kind': 'initial'})
        raise ReachedExecution()
    monkeypatch.setattr(single_unified, 'run_unified_feedback', enter)
    with pytest.raises(ReachedExecution):
        single_unified.run_unified_single(build_synthetic_case(8), SimpleNamespace(
            microgrids={'SC': {'storage_energy_mwh': [2.5] * 9}}), 'SC',
            storage_enabled=True, time_limit_seconds=60, packaged_inputs=True, events=[],
            policy=single_execution_policy(None), live=lambda event: captured.update(event=event))
    assert captured['domain'] == preview.domain
    assert captured['initial'] == preview.initial
    assert captured['total'] == preview.total == 1440
    assert captured['event']['input_forecast'] == preview.input_forecast.model_dump(mode='json')
    assert preview.input_forecast.step_minutes == preview.update_steps
