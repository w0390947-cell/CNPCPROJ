"""Solver-free AC restoration policy and objective-specific evidence."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


ACRecoveryPhase = Literal["physical_feasibility", "original_objective"]


class ACRecoveryPolicy(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", allow_inf_nan=False)
    version: Literal["ac-feasibility-recovery-v1"] = "ac-feasibility-recovery-v1"
    maximum_seconds: float = Field(default=60.0, gt=0, le=1800)
    feasibility_fraction: float = Field(default=0.4, gt=0, lt=1)
    allow_feasible_adoption: bool = True


class ACRecoveryAttempt(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", allow_inf_nan=False)
    phase: ACRecoveryPhase
    budget_seconds: float = Field(gt=0)
    elapsed_seconds: float = Field(ge=0)
    network_physics: Literal["branch-flow-equality"] = "branch-flow-equality"
    objective: Literal[
        "constant_feasibility", "original_economic", "joint_lexicographic"
    ]
    solver_status: str
    solver_feasible: bool
    certified: bool
    checks: dict[str, bool] = Field(default_factory=dict)
    metrics: dict[str, float | None] = Field(default_factory=dict)
    original_objective_cny: float | None = None
    solver_objective_value: float | None = None
    relative_gap: float | None = Field(default=None, ge=0)
    target_relative_gap: float = Field(default=1e-4, gt=0, lt=1)
    reference_distance: float | None = Field(default=None, ge=0)
    original_optimality_proven: bool = False
    p_tracking_penalty_cny_per_mw: float = Field(default=20000.0, ge=0)
    q_tracking_penalty_cny_per_mvar: float = Field(default=8000.0, ge=0)
    extra_loss_penalty_cny_per_mwh: float = Field(default=0.0, ge=0, le=0)
    voltage_security_margin_pu: float = Field(default=0.0, ge=0, le=0)
    infeasibility_scope: Literal["none", "original_model", "restricted_model"] = "none"
    reason: str = ""

    @model_validator(mode="after")
    def valid_certificate(self) -> "ACRecoveryAttempt":
        if (self.phase == "physical_feasibility") != (
            self.objective == "constant_feasibility"
        ):
            raise ValueError("restoration phase must identify its actual objective")
        if self.certified and (
            not self.solver_feasible
            or not self.checks
            or not all(self.checks.values())
            or self.original_objective_cny is None
        ):
            raise ValueError("AC restoration requires complete independent checks")
        if self.original_optimality_proven and (
            not self.certified
            or self.phase != "original_objective"
            or self.relative_gap is None
            or self.relative_gap > self.target_relative_gap
            or self.solver_status not in {"optimal", "gaplimit", "timelimit"}
        ):
            raise ValueError("feasibility objective cannot prove economic optimality")
        if self.infeasibility_scope != "none" and self.solver_feasible:
            raise ValueError("an incumbent cannot certify infeasibility")
        return self


class ACRecoveryEvidence(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", allow_inf_nan=False)
    version: Literal["ac-feasibility-recovery-v1"] = "ac-feasibility-recovery-v1"
    policy: ACRecoveryPolicy
    trigger: str
    budget_seconds: float = Field(gt=0)
    elapsed_seconds: float = Field(ge=0)
    attempts: tuple[ACRecoveryAttempt, ...] = ()
    # Ordered fast-path observations. Nonfinite metrics are represented by null.
    fast_feedback: tuple[dict[str, str | bool | int | float | None], ...] = ()
    selected_attempt: int | None = Field(default=None, ge=1)
    outcome: Literal["certified", "model_infeasible", "unresolved"]
    original_optimality: Literal["proven", "unproven"] = "unproven"

    @model_validator(mode="after")
    def valid_selection(self) -> "ACRecoveryEvidence":
        if self.selected_attempt is not None:
            if self.selected_attempt > len(self.attempts):
                raise ValueError("missing selected restoration attempt")
            selected = self.attempts[self.selected_attempt - 1]
            if self.outcome != "certified" or not selected.certified:
                raise ValueError(
                    "only an independently certified candidate can be selected"
                )
            if (
                self.original_optimality == "proven"
            ) != selected.original_optimality_proven:
                raise ValueError(
                    "optimality evidence must belong to the selected candidate"
                )
        elif self.outcome == "certified" or self.original_optimality == "proven":
            raise ValueError("certification requires a selected attempt")
        if self.outcome == "model_infeasible" and not any(
            a.infeasibility_scope == "original_model" for a in self.attempts
        ):
            raise ValueError("infeasibility needs original-model evidence")
        return self

    @property
    def permits_feasible_adoption(self) -> bool:
        return self.outcome == "certified" and self.policy.allow_feasible_adoption
