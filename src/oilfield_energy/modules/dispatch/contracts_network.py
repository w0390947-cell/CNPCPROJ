"""Network-aware coordination and bounded joint restoration evidence.

The convex reference model relaxes storage modes and branch-flow equalities.
Only downstream physical certification authorizes adoption; see
docs/modeling/Network_Physical_Recovery.md.
"""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .contracts_ac_recovery import ACRecoveryEvidence


class NetworkCoordinationPolicy(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", allow_inf_nan=False)
    version: Literal["network-physical-coordination-v1"] = (
        "network-physical-coordination-v1"
    )
    max_joint_attempts: int = Field(default=2, ge=1, le=4, strict=True)
    joint_time_limit_seconds: float = Field(default=60.0, gt=0, le=1800)
    # Dimensionless sum of P/Q errors normalized by the engineering tracking bands.
    deviation_tolerance: float = Field(default=1e-5, gt=0, le=0.01)
    relative_gap: float = Field(default=1e-7, gt=0, le=1e-4)


class JointNetworkTarget(BaseModel):
    """Solver-independent lexicographic restoration parameters for one window."""

    model_config = ConfigDict(frozen=True, extra="forbid", allow_inf_nan=False)
    pf_target: float = Field(gt=0, le=1)
    pf_margin_mvar: tuple[float, ...]
    p_scale_mw: float = Field(gt=0)
    q_scale_mvar: float = Field(gt=0)
    deviation_tolerance: float = Field(gt=0)

    @model_validator(mode="after")
    def nonnegative_margin(self) -> "JointNetworkTarget":
        if not self.pf_margin_mvar or any(v < 0 for v in self.pf_margin_mvar):
            raise ValueError("joint recovery requires aligned nonnegative PF margins")
        return self


class JointNetworkEvidence(BaseModel):
    """A bounded solve record; failure never proves original AC infeasibility."""

    model_config = ConfigDict(frozen=True, extra="forbid", allow_inf_nan=False)
    version: Literal["joint-network-recovery-v1"] = "joint-network-recovery-v1"
    status: Literal["certified", "model_infeasible", "unresolved", "error"]
    reason: str = ""
    time_limit_seconds: float = Field(gt=0)
    elapsed_seconds: float = Field(ge=0)
    source_p_mw: dict[str, tuple[float, ...]]
    source_q_mvar: dict[str, tuple[float, ...]]
    time_minutes: tuple[float, ...]
    pf_margin_mvar: tuple[float, ...]
    deviation: float | None = Field(default=None, ge=0)
    deviation_bound: float | None = None
    deviation_optimal: bool = False
    ac_iterations: int = Field(default=0, ge=0)
    ac_recovery: ACRecoveryEvidence | None = None
    storage_integrality_certified: bool = False
    solver_status: str = ""
    solver_relative_gap: float = Field(default=1e-7, gt=0)
    p_mw: dict[str, tuple[float, ...]] = Field(default_factory=dict)
    q_mvar: dict[str, tuple[float, ...]] = Field(default_factory=dict)

    @model_validator(mode="after")
    def complete(self) -> "JointNetworkEvidence":
        n = len(self.time_minutes)
        if (
            not n
            or not self.source_p_mw
            or set(self.source_p_mw) != set(self.source_q_mvar)
            or len(self.pf_margin_mvar) != n
            or any(v < 0 for v in self.pf_margin_mvar)
            or any(
                len(v) != n
                for v in (*self.source_p_mw.values(), *self.source_q_mvar.values())
            )
            or any(b <= a for a, b in zip(self.time_minutes, self.time_minutes[1:]))
        ):
            raise ValueError("joint evidence requires a complete aligned source window")
        if self.status == "certified" and (
            self.ac_iterations < 1
            or not self.storage_integrality_certified
            or self.deviation is None
            or set(self.p_mw) != set(self.source_p_mw)
            or set(self.q_mvar) != set(self.source_q_mvar)
            or any(len(v) != n for v in (*self.p_mw.values(), *self.q_mvar.values()))
        ):
            raise ValueError("certified joint plan needs AC and storage mode evidence")
        return self
