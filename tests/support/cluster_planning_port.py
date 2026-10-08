"""Execution-boundary spy for real solver/recovery integration tests.

These tests exercise planning only. Full continuation, device and AC acceptance
is independently covered by the real supervisor and service integration tests.
"""

from types import SimpleNamespace

from oilfield_energy.bootstrap.adapters import cluster_rolling
from oilfield_energy.bootstrap.adapters.cluster_safety import ClusterSafetyPort
from oilfield_energy.modules.control.contracts import DynamicTrackingPolicy
from oilfield_energy.workflows.cluster_execution.contracts import ClusterSafetyPolicy
from tests.support.cluster_safety import observed


def install_planning_port(monkeypatch, policy):
    class PlanningPort(ClusterSafetyPort):
        def __init__(self, case, policy, storage_enabled, plants, *, domain):
            self.domain = domain
            self.plans = {}
            self.plan_start = 0
            self.candidates = []
            self.controller = SimpleNamespace(
                minute=0,
                states=tuple(r.initial for r in domain.regions),
                tasks={},
                task_history=[],
                observations=[],
                commands_for_endpoints=lambda targets: targets,
                certify_candidate=self.candidates.append,
            )

        def advance(self, *args, **kwargs):
            raise AssertionError("candidate preparation must not advance execution")

        def observed(self, minute):
            return observed(self.domain, minute)

    monkeypatch.setattr(cluster_rolling, "ClusterSafetyPort", PlanningPort)
    return policy.model_copy(
        update={
            "cluster_safety": ClusterSafetyPolicy(),
            "dynamic_tracking": policy.dynamic_tracking or DynamicTrackingPolicy(),
        }
    )
