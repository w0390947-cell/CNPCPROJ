"""One execution chain and its separately reported safety/tracking evidence."""

from typing import Literal

from pydantic import Field, model_validator

from oilfield_energy.modules.control.contracts import (
    BalancePolicy,
    TubeBox,
    TubeCommand,
    TubeDeviceModel,
    TubeState,
)
from oilfield_energy.modules.dispatch.contracts import CostRates, PQPlanningWitness
from oilfield_energy.modules.power_flow.contracts import PowerInterval, RadialEnvelope
from oilfield_energy.workflows.cluster_execution.contracts import RegionalExecution

from .contracts_domain import BoundedBusLoad, FirmGeneration, SingleValue
from .contracts_tasks import HardTaskAssessment, TaskContinuation, TerminalEnergyTask


class SingleResearchPolicy(SingleValue, frozen=True):
    version: Literal["single-research-v3"] = "single-research-v3"
    source: str = "Configurable research bounds for the seeded synthetic study; not field-validated limits"
    load_relative_error: float = Field(default=0.025, ge=0, lt=1)
    wind_nameplate_error: float = Field(default=0.05, ge=0, lt=1)
    pv_nameplate_error: float = Field(default=0.15, ge=0, lt=1)
    settling_resolution: float = Field(default=0.0001, gt=0, le=0.001)
    backup_maximum_steps: int = Field(default=90, ge=10, le=240)
    planning_pf_tangent_fraction: float = Field(default=0.6, gt=0, le=1)
    forecast_interpolation: Literal["linear", "hold"] = "linear"
    storage_tracking_reserve_mwh: float = Field(default=0.002, ge=0)
    energy_reference_cost_cny_per_mwh: float = Field(default=50, ge=0)
    certified_economic_candidates: int = Field(default=3, ge=1, le=8)
    allocation_search_budget: int = Field(default=6, ge=1, le=32)
    renewable_command_reserve_mw: float = Field(default=0.02, ge=0)
    reactive_regulator_reference_fraction: float = Field(default=0.7, gt=0, lt=1)
    terminal_persistence: Literal["last_forecast_envelope"] = "last_forecast_envelope"
    within_step_disturbance: Literal["zero_order_hold"] = "zero_order_hold"


class SingleForecastEnvelope(SingleValue, frozen=True):
    loads: tuple[BoundedBusLoad, ...]
    generation: tuple[FirmGeneration, ...]


class UnifiedDomain(SingleValue, frozen=True):
    version: Literal["unified-single-v7"] = "unified-single-v7"
    research: SingleResearchPolicy
    bound_source: str = Field(default="Explicit research domain", min_length=1)
    network: RadialEnvelope
    devices: TubeDeviceModel
    forecasts: tuple[SingleForecastEnvelope, ...]
    terminal_command: TubeCommand
    p_band_mw: float = Field(gt=0)
    q_band_mvar: float = Field(gt=0)
    response_steps: int = Field(ge=1)
    context_id: str
    energy_task: TerminalEnergyTask | None = None
    economic_prices_cny_per_mwh: tuple[float, ...] = ()
    economic_rates: CostRates | None = None

    @model_validator(mode="after")
    def coverage(self) -> "UnifiedDomain":
        if not self.forecasts or self.devices.version != "unified-device-v1":
            raise ValueError(
                "unified execution requires its explicit physical model and forecast bounds"
            )
        buses = set(self.network.buses)
        if self.economic_rates is not None and len(
            self.economic_prices_cny_per_mwh
        ) != len(self.forecasts):
            raise ValueError("economic prices must cover the exact forecast clock")
        renewable = {
            r.resource_id for r in self.devices.resources if r.kind in ("wind", "pv")
        }
        for forecast in self.forecasts:
            if (
                len(forecast.loads) != len(buses)
                or {b.bus for b in forecast.loads} != buses
            ):
                raise ValueError("forecast load identities differ")
            if (
                len(forecast.generation) != len(renewable)
                or {r.resource_id for r in forecast.generation} != renewable
            ):
                raise ValueError("forecast generation identities differ")
        return self


class AllocationTrial(SingleValue, frozen=True):
    construction: int = Field(ge=0)
    status: Literal["certified", "construction_unproven"]
    reason: str = ""
    intervention: float | None = Field(default=None, ge=0)
    economic_cost_cny: float | None = None

    @model_validator(mode="after")
    def evidence(self) -> "AllocationTrial":
        if self.status == "certified":
            if self.intervention is None or self.reason:
                raise ValueError("certified allocation requires a score and no failure")
        elif (
            not self.reason
            or self.intervention is not None
            or self.economic_cost_cny is not None
        ):
            raise ValueError(
                "unproven allocation requires its failure, not a fabricated score"
            )
        return self


class TargetTrackingCertificate(SingleValue, frozen=True):
    scope: Literal["finite_forecast_box_feedback"] = "finite_forecast_box_feedback"
    target_p_mw: float
    target_q_mvar: float
    deadline_step: int = Field(ge=0)
    guaranteed_until_step: int = Field(gt=0)
    nominal: TubeCommand
    construction: int = Field(default=0, ge=0)
    allocation_trials: tuple[AllocationTrial, ...] = ()
    policies: tuple[BalancePolicy, ...]
    tubes: tuple[TubeBox, ...]
    pcc_p_mw: tuple[PowerInterval | None, ...]
    pcc_q_mvar: tuple[PowerInterval | None, ...]

    @model_validator(mode="after")
    def dimensions(self) -> "TargetTrackingCertificate":
        n = len(self.policies)
        if (
            not n
            or len(self.tubes) != n + 1
            or len(self.pcc_p_mw) != n
            or len(self.pcc_q_mvar) != n
        ):
            raise ValueError("tracking certificate axes differ")
        if self.allocation_trials:
            certified = [x for x in self.allocation_trials if x.status == "certified"]
            if (
                tuple(x.construction for x in self.allocation_trials)
                != tuple(range(len(self.allocation_trials)))
                or not certified
                or self.construction
                != min(
                    certified,
                    key=lambda x: (
                        x.intervention if x.intervention is not None else float("inf"),
                        x.economic_cost_cny or 0,
                        x.construction,
                    ),
                ).construction
            ):
                raise ValueError(
                    "allocation selection differs from certified intervention/cost evidence"
                )
        return self


class UnifiedPlanCertificate(SingleValue, frozen=True):
    start_step: int = Field(ge=0)
    initial: TubeBox
    commands: tuple[TubeCommand, ...]
    terminal: TubeBox
    network_proof_digest: str
    tracking: TargetTrackingCertificate | None = None
    task_continuation: TaskContinuation | None = None
    task_reason: str = ""

    @model_validator(mode="after")
    def feedback_prefix(self) -> "UnifiedPlanCertificate":
        if self.task_continuation is not None and self.task_reason:
            raise ValueError("a certified task cannot carry a certification failure")
        t = self.tracking
        if t is not None and (
            t.tubes[0] != self.initial
            or t.guaranteed_until_step != self.start_step + len(t.policies)
            or tuple(p.base for p in t.policies) != self.commands[: len(t.policies)]
        ):
            raise ValueError("tracking policy differs from the certified prefix")
        return self


class TargetAdmission(SingleValue, frozen=True):
    start_step: int = Field(ge=0)
    end_step: int = Field(gt=0)
    origin: Literal["economic", "external", "retained"]
    requested_p_mw: float
    requested_q_mvar: float
    nominal: TubeCommand
    attempted_targets: tuple[tuple[float, float], ...] = ()
    plan: UnifiedPlanCertificate | None = None
    reason: str = ""
    candidate_economic_costs_cny: tuple[float | None, ...] = ()
    candidate_outcomes: tuple[Literal["certified", "construction_unproven"], ...] = ()
    candidate_reasons: tuple[str, ...] = ()
    selected_candidate: int | None = Field(default=None, ge=0)
    economic_scope: Literal["firm_forecast_midpoint_feedback_prefix"] = (
        "firm_forecast_midpoint_feedback_prefix"
    )
    selection_rule: Literal["original_target_then_intervention_then_cost"] = (
        "original_target_then_intervention_then_cost"
    )

    @property
    def accepted(self) -> bool:
        return self.plan is not None

    @model_validator(mode="after")
    def decision(self) -> "TargetAdmission":
        if self.selected_candidate is not None and self.selected_candidate >= len(
            self.attempted_targets
        ):
            raise ValueError("selected target candidate is missing")
        if len(self.candidate_outcomes) != len(self.attempted_targets) or len(
            self.candidate_reasons
        ) != len(self.attempted_targets):
            raise ValueError("each target search requires its outcome and diagnostic")
        if any(
            (outcome == "certified") == bool(reason)
            for outcome, reason in zip(
                self.candidate_outcomes, self.candidate_reasons, strict=True
            )
        ):
            raise ValueError("target outcome differs from its certification diagnostic")
        if (
            self.candidate_outcomes
            and self.candidate_outcomes[0] == "certified"
            and (self.selected_candidate != 0 or len(self.attempted_targets) != 1)
        ):
            raise ValueError(
                "a certified original target excludes alternate-target search"
            )
        if (
            self.selected_candidate is not None
            and self.candidate_outcomes[self.selected_candidate] != "certified"
        ):
            raise ValueError("selected target has no certificate")
        if self.plan is None and self.selected_candidate is not None:
            raise ValueError("rejected target cannot have a selected candidate")
        if (self.plan is not None) != ("certified" in self.candidate_outcomes):
            raise ValueError(
                "target adoption differs from search certification outcomes"
            )
        if self.plan is not None and self.selected_candidate is None:
            raise ValueError("accepted target requires a selected candidate")
        if self.candidate_economic_costs_cny and len(
            self.candidate_economic_costs_cny
        ) != len(self.attempted_targets):
            raise ValueError("candidate costs must match attempted targets")
        if self.selected_candidate is not None and self.selected_candidate >= len(
            self.attempted_targets
        ):
            raise ValueError("selected target candidate is missing")
        if self.end_step <= self.start_step:
            raise ValueError("target admission horizon is empty")
        if not self.attempted_targets or (self.plan is None and not self.reason):
            raise ValueError(
                "target admission requires attempts and a rejection reason"
            )
        if self.origin in ("external", "retained") and self.attempted_targets != (
            (self.requested_p_mw, self.requested_q_mvar),
        ):
            raise ValueError("external target cannot be rewritten during admission")
        if self.plan is not None:
            ranked = [
                (cost, i)
                for i, cost in enumerate(self.candidate_economic_costs_cny)
                if cost is not None
            ]
            expected = (
                0
                if ranked and self.candidate_economic_costs_cny[0] is not None
                else min(ranked)[1]
                if ranked
                else self.selected_candidate
            )
            if ranked and self.selected_candidate != expected:
                raise ValueError(
                    "selected candidate violates original-target priority or forecast cost ordering"
                )
            t = self.plan.tracking
            if (
                t is None
                or self.plan.start_step != self.start_step
                or t.guaranteed_until_step != self.end_step
            ):
                raise ValueError(
                    "target admission has no matching tracking certificate"
                )
            pair = (t.target_p_mw, t.target_q_mvar)
            if (
                not self.attempted_targets
                or self.attempted_targets[
                    self.selected_candidate
                    if self.selected_candidate is not None
                    else -1
                ]
                != pair
                or t.nominal != self.nominal
            ):
                raise ValueError("accepted target differs from its recorded search")
            if self.origin in ("external", "retained") and pair != (
                self.requested_p_mw,
                self.requested_q_mvar,
            ):
                raise ValueError("external target cannot be rewritten")
        return self


class SingleEconomicProposal(SingleValue, frozen=True):
    start_step: int = Field(ge=0)
    initial: TubeState
    command: TubeCommand | None = None
    target_p_mw: float
    target_q_mvar: float
    result: RegionalExecution
    planning_pf_targets: tuple[float, ...] = ()
    pq_witness: PQPlanningWitness | None = None
    target_origin: Literal["economic", "external"] = "economic"


class UnifiedStep(SingleValue, frozen=True):
    step: int = Field(ge=0)
    before: TubeState
    after: TubeState
    command: TubeCommand
    proposed_command: TubeCommand | None = None
    candidate_fraction: float = Field(default=0, ge=0, le=1)
    observed: SingleForecastEnvelope
    disposition: Literal["economic", "retained_feedback", "backup", "terminal"]
    proposal_reason: str = ""
    plan_index: int = Field(ge=0)
    plan_offset: int = Field(ge=0)
    requested_p_mw: float
    requested_q_mvar: float
    deadline_step: int = Field(ge=0)
    pcc_p_mw: PowerInterval
    pcc_q_mvar: PowerInterval
    tracking_certified: bool


class UnifiedExecution(SingleValue, frozen=True):
    version: Literal["unified-single-v7"] = "unified-single-v7"
    domain: UnifiedDomain
    initial: TubeState
    status: Literal["passed", "unproven", "assumption_violated"]
    reason: str = ""
    requested_steps: int = Field(ge=1)
    update_steps: int = Field(default=15, ge=1)
    steps: tuple[UnifiedStep, ...] = ()
    plans: tuple[UnifiedPlanCertificate, ...] = ()
    terminal_proof_digest: str
    tracking_status: Literal["passed", "violated", "not_assessed"]
    tracking_scope: Literal["completed_cycle_pcc"] = "completed_cycle_pcc"
    economic_proposals: tuple[SingleEconomicProposal, ...] = ()
    target_admissions: tuple[TargetAdmission, ...] = ()
    target_admission_status: Literal["passed", "rejected", "not_assessed"] = (
        "not_assessed"
    )
    hard_task: HardTaskAssessment = HardTaskAssessment(
        guarantee="not_required", outcome="not_required"
    )

    @model_validator(mode="after")
    def chain(self) -> "UnifiedExecution":
        if len({p.start_step for p in self.economic_proposals}) != len(
            self.economic_proposals
        ):
            raise ValueError("duplicate economic proposal")
        if len({a.start_step for a in self.target_admissions}) != len(
            self.target_admissions
        ):
            raise ValueError("duplicate target admission")
        admission_status = (
            "not_assessed"
            if not self.target_admissions
            else "passed"
            if all(a.accepted for a in self.target_admissions)
            else "rejected"
        )
        if self.target_admission_status != admission_status:
            raise ValueError("target acceptance conclusion differs from its evidence")
        task = self.domain.energy_task
        if task is None:
            if (
                self.hard_task.guarantee != "not_required"
                or self.hard_task.outcome != "not_required"
            ):
                raise ValueError("hard task assessment has no declared task")
        else:
            initial_certified = bool(
                self.plans and self.plans[0].task_continuation is not None
            )
            maintained = initial_certified and all(
                p.task_continuation is not None for p in self.plans
            )
            guarantee = (
                "assumption_violated"
                if self.status == "assumption_violated"
                else "maintained"
                if maintained
                else "unproven"
            )
            outcome = "pending"
            if len(self.steps) >= task.deadline_step:
                energy = self.steps[task.deadline_step - 1].after.energy_mwh
                outcome = (
                    "met"
                    if task.energy_mwh.low <= energy <= task.energy_mwh.high
                    else "missed"
                )
            if (
                self.hard_task.guarantee != guarantee
                or self.hard_task.outcome != outcome
                or self.hard_task.initial_certified != initial_certified
                or any(
                    p.task_continuation.task != task
                    for p in self.plans
                    if p.task_continuation is not None
                )
            ):
                raise ValueError("hard task assessment differs from its evidence")
        state = self.initial
        for k, row in enumerate(self.steps):
            if (
                row.step != k
                or row.before != state
                or row.plan_index >= len(self.plans)
            ):
                raise ValueError("unified evidence has a causal gap")
            plan = self.plans[row.plan_index]
            if row.plan_offset != k - plan.start_step:
                raise ValueError("backup clock was reset")
            command = (
                plan.commands[row.plan_offset]
                if row.plan_offset < len(plan.commands)
                else self.domain.terminal_command
            )
            feedback_policy = plan.tracking is not None and row.plan_offset < len(
                plan.tracking.policies
            )
            recovery = plan.task_continuation
            recovery_offset = k - recovery.recovery_start_step if recovery else -1
            energy_feedback = recovery is not None and 0 <= recovery_offset < len(
                recovery.policies
            )
            if not feedback_policy and not energy_feedback and row.command != command:
                raise ValueError("executed command differs from its safety certificate")
            if energy_feedback:
                assert recovery is not None
                policy = recovery.policies[recovery_offset]
                if row.command.q_mvar != policy.base.q_mvar or any(
                    a != b
                    for a, b, r in zip(
                        row.command.p_mw,
                        policy.base.p_mw,
                        self.domain.devices.resources,
                        strict=True,
                    )
                    if r.kind != "storage"
                ):
                    raise ValueError(
                        "executed command differs from its energy feedback"
                    )
            if feedback_policy:
                assert plan.tracking is not None
                policy = plan.tracking.policies[row.plan_offset]
                for actual, base, weights in (
                    (row.command.p_mw, policy.base.p_mw, policy.weights(False)),
                    (row.command.q_mvar, policy.base.q_mvar, policy.weights(True)),
                ):
                    if len(actual) != len(base) or any(
                        a != b
                        for i, (a, b) in enumerate(zip(actual, base, strict=True))
                        if not weights[i]
                    ):
                        raise ValueError(
                            "executed command differs from its feedback policy"
                        )
            state = row.after
        if self.status == "passed" and (
            len(self.steps) != self.requested_steps or not self.plans
        ):
            raise ValueError("safety success requires the complete executed chain")
        if self.tracking_status == "passed" and (
            not any(r.step + 1 >= r.deadline_step for r in self.steps)
            or any(
                not r.tracking_certified
                for r in self.steps
                if r.step + 1 >= r.deadline_step
            )
        ):
            raise ValueError("tracking success lacks deadline evidence")
        for proposal in self.economic_proposals:
            expected = (
                self.steps[proposal.start_step].before
                if proposal.start_step < len(self.steps)
                else state
            )
            if proposal.start_step > len(self.steps) or proposal.initial != expected:
                raise ValueError(
                    "economic replanning did not inherit the actual completed state"
                )
        return self
