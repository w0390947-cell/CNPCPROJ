"""Immutable, explicitly scoped synchronous PCC import assessment values."""

from dataclasses import dataclass
from math import isfinite
from typing import Literal

from .contracts_envelope import (
    ACBoxCertificate as ACBoxCertificate, EnvelopeBranch as EnvelopeBranch,
    PowerInterval as PowerInterval, RadialEnvelope as RadialEnvelope,
)

AssessmentStatus = Literal["passed", "violated", "unknown", "not_computed"]
ClusterStage = Literal["centralized_reference", "admm_reference", "day_ahead", "intraday", "minute"]


@dataclass(frozen=True)
class FlowNumerics:
    """Independent step, equation-residual and singularity tolerances (pu)."""

    voltage_step_tolerance_pu: float = 1e-10
    power_balance_tolerance_pu: float = 1e-8
    singular_voltage_pu: float = 1e-8

    def __post_init__(self) -> None:
        for value in (
            self.voltage_step_tolerance_pu,
            self.power_balance_tolerance_pu,
            self.singular_voltage_pu,
        ):
            if isinstance(value, bool) or not isfinite(value) or value <= 0:
                raise ValueError("flow numerical tolerances must be finite and positive")


@dataclass(frozen=True)
class RadialBalanceBranch:
    """Indices refer to one common bus axis; impedance is on the child side."""

    parent_index: int
    child_index: int
    impedance_pu: complex
    tap_ratio: float


@dataclass(frozen=True)
class PowerBalanceResidual:
    maximum_p_pu: float
    maximum_q_pu: float

    @property
    def maximum_pu(self) -> float:
        return max(self.maximum_p_pu, self.maximum_q_pu)


@dataclass(frozen=True)
class PointInputValidation:
    point_id: str
    source: str
    reasons: tuple[str, ...]

    @property
    def valid(self) -> bool:
        return not self.reasons


class SingularPowerVoltage(FloatingPointError):
    """A nonzero constant-power demand cannot be evaluated at this voltage."""


@dataclass(frozen=True)
class RegionalImport:
    """Positive import in MW, elapsed minutes on a shared simulation origin.

    Tuple inputs own values. `valid` describes physical/measurement validity,
    not whether a known operating point violates an engineering constraint.
    """

    name: str
    time_minutes: tuple[float, ...]
    import_mw: tuple[float, ...]
    valid: tuple[bool, ...]


@dataclass(frozen=True)
class ClusterImportAssessment:
    stage: ClusterStage
    status: AssessmentStatus
    limit_mw: float
    tolerance_mw: float
    peak_import_mw: float | None
    violation_steps: int | None
    sample_count: int
    reason: str


@dataclass(frozen=True)
class ClusterValidation:
    """A reference-only pass never certifies uncomputed execution stages."""

    scope: Literal["reference_only", "hierarchical_execution"]
    assessed_scope_status: AssessmentStatus
    execution_status: AssessmentStatus
    stages: tuple[ClusterImportAssessment, ...]
    version: Literal["cluster-validation-v1"] = "cluster-validation-v1"


@dataclass(frozen=True)
class ClusterPowerFactorPolicy:
    """Per-slot positive-import plan target, not a contractual time average."""

    target: float
    numerical_tolerance: float = 1e-9  # Dimensionless PF comparison tolerance.
    version: Literal["cluster-plan-power-factor-v1"] = "cluster-plan-power-factor-v1"

    def __post_init__(self) -> None:
        if isinstance(self.target, bool) or not isfinite(self.target) or not 0 < self.target <= 1:
            raise ValueError("power factor target must be in (0, 1]")
        if (
            isinstance(self.numerical_tolerance, bool)
            or not isfinite(self.numerical_tolerance)
            or not 0 <= self.numerical_tolerance < self.target
            or self.version != "cluster-plan-power-factor-v1"
        ):
            raise ValueError("invalid power factor numerical policy")


@dataclass(frozen=True)
class RegionalPCCPower:
    name: str
    time_minutes: tuple[float, ...]
    p_mw: tuple[float, ...]
    q_mvar: tuple[float, ...]  # Signed PCC import; sum before taking magnitude.
    valid: tuple[bool, ...]


@dataclass(frozen=True)
class ClusterPowerFactorPoint:
    time_minutes: float
    p_mw: float
    q_mvar: float
    power_factor: float | None
    status: AssessmentStatus
    reason: str


@dataclass(frozen=True)
class ClusterPowerFactorAssessment:
    stage: ClusterStage
    policy: ClusterPowerFactorPolicy
    status: AssessmentStatus
    points: tuple[ClusterPowerFactorPoint, ...]
    reason: str
