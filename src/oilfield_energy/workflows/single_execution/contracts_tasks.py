"""Absolute hard tasks and evidence for their retained executable continuation."""

from typing import Literal

from pydantic import Field, model_validator

from oilfield_energy.modules.control.contracts import EnergyReturnPolicy, TubeBox
from oilfield_energy.modules.power_flow.contracts import PowerInterval

from .contracts_domain import SingleValue


class TerminalEnergyTask(SingleValue, frozen=True):
    task_id: str = Field(min_length=1)
    deadline_step: int = Field(gt=0)
    energy_mwh: PowerInterval
    source: str = Field(min_length=1)


class TaskContinuation(SingleValue, frozen=True):
    task: TerminalEnergyTask
    recovery_start_step: int = Field(ge=0)
    policies: tuple[EnergyReturnPolicy, ...] = ()
    tubes: tuple[TubeBox, ...]
    deadline_energy_mwh: PowerInterval

    @model_validator(mode="after")
    def coverage(self) -> "TaskContinuation":
        if len(self.tubes) != len(self.policies) + 1 or not (
            self.task.energy_mwh.low
            <= self.deadline_energy_mwh.low
            <= self.deadline_energy_mwh.high
            <= self.task.energy_mwh.high
        ):
            raise ValueError("hard task continuation has no matching energy evidence")
        return self


class HardTaskAssessment(SingleValue, frozen=True):
    guarantee: Literal["not_required", "unproven", "maintained", "assumption_violated"]
    outcome: Literal["not_required", "pending", "met", "missed"]
    initial_certified: bool = False
    reason: str = ""
