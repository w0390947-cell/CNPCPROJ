"""The retired execution authority must not reappear in the distributable package."""

import importlib.util

import pytest


@pytest.mark.parametrize(
    "module",
    [
        "oilfield_energy.device_control",
        "oilfield_energy.bootstrap.adapters.cluster_reserve_delivery",
        "oilfield_energy.workflows.cluster_execution.transition",
        "oilfield_energy.workflows.cluster_execution.reserve_admission",
        "oilfield_energy.modules.control.application_transition",
    ],
)
def test_retired_execution_modules_are_not_importable(module):
    assert importlib.util.find_spec(module) is None
