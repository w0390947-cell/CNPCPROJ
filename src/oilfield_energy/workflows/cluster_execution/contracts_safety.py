"""Explicit research domain and replayable joint continuation certificates."""

# pyright: strict
from typing import Literal
from pydantic import BaseModel, ConfigDict, Field, model_validator
from oilfield_energy.modules.control.contracts import (
    TubeBox,
    TubeCommand,
    TubeDeviceModel,
    TubeState,
)
from oilfield_energy.modules.power_flow.contracts import (
    RadialEnvelope,
    PowerInterval,
    ACBoxCertificate,
)
from oilfield_energy.modules.dispatch.contracts import (
    ReserveDeliveryPolicy,
    StorageReservePolicy,
)
from .contracts_reserve import ReserveClusterCertificate, ReserveActivationCommand


class SafetyValue(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", allow_inf_nan=False)


class ClusterSafetyPolicy(SafetyValue):
    version: Literal["cluster-continuation-v1"] = "cluster-continuation-v1"
    load_relative_error: float = Field(default=0.025, ge=0, lt=1)
    wind_nameplate_error: float = Field(default=0.05, ge=0, lt=1)
    pv_nameplate_error: float = Field(default=0.15, ge=0, lt=1)
    maximum_return_minutes: int = Field(default=90, ge=1, le=240)
    within_minute: Literal["zero_order_hold"] = "zero_order_hold"
    device_intersample: Literal["linear_all_channels"] = "linear_all_channels"
    initial_condition: Literal["case_energy_idle_resources"] = (
        "case_energy_idle_resources"
    )
    terminal_boundary: Literal["last_declared_envelope_persists"] = (
        "last_declared_envelope_persists"
    )
    source: str = (
        "可配置软件研究边界：负荷±2.5%，风电/光伏可用下界扣除额定5%/15%；非现场定值"
    )


class ClusterForecast(SafetyValue):
    load_p_mw: tuple[PowerInterval, ...]
    load_q_mvar: tuple[PowerInterval, ...]
    available_mw: tuple[float, ...]


class ClusterRegionDomain(SafetyValue):
    region: str
    network: RadialEnvelope
    devices: TubeDeviceModel
    forecasts: tuple[ClusterForecast, ...]
    initial: TubeState
    required_reserve_mw: float = Field(ge=0)
    recovery_deadline_minute: int = Field(ge=0)

    @model_validator(mode="after")
    def axes(self) -> "ClusterRegionDomain":
        n, b = len(self.devices.resources), len(self.network.buses)
        if (
            not self.forecasts
            or self.devices.version != "cluster-device-v1"
            or self.devices.step_minutes != 1
            or any(x.bus not in self.network.buses for x in self.devices.resources)
            or len(self.initial.p_mw) != n
            or len(self.initial.q_mvar) != n
            or any(
                len(f.load_p_mw) != b
                or len(f.load_q_mvar) != b
                or len(f.available_mw) != n
                for f in self.forecasts
            )
        ):
            raise ValueError("cluster safety domain axes/model differ")
        if any(a < 0 for f in self.forecasts for a in f.available_mw):
            raise ValueError("negative generation availability")
        return self


class ClusterSafetyDomain(SafetyValue):
    policy: ClusterSafetyPolicy
    regions: tuple[ClusterRegionDomain, ...]
    import_limit_mw: float = Field(gt=0)
    pf_target: float = Field(gt=0, le=1)
    reserve_policy: StorageReservePolicy | None = None
    delivery_policy: ReserveDeliveryPolicy | None = None
    context_id: str

    @model_validator(mode="after")
    def identities(self) -> "ClusterSafetyDomain":
        if (
            not self.regions
            or len({r.region for r in self.regions}) != len(self.regions)
            or len({len(r.forecasts) for r in self.regions}) != 1
        ):
            raise ValueError("cluster domain requires aligned unique regions")
        if (self.reserve_policy is None) != (self.delivery_policy is None):
            raise ValueError("reserve product and delivery policy must be paired")
        return self


class JointPathCertificate(SafetyValue):
    minute: int = Field(ge=0)
    endpoints: tuple[TubeBox, ...]
    paths: tuple[TubeBox, ...]
    networks: tuple[ACBoxCertificate, ...]
    pcc_p_mw: PowerInterval
    pcc_q_mvar: PowerInterval

    @model_validator(mode="after")
    def complete(self) -> "JointPathCertificate":
        if (
            not self.paths
            or len(self.paths) != len(self.endpoints)
            or len(self.networks) != len(self.paths)
            or any(not x.certified for x in self.networks)
        ):
            raise ValueError("joint path requires every regional network certificate")
        return self


class ClusterContinuation(SafetyValue):
    context_id: str
    start_minute: int
    initial: tuple[TubeBox, ...]
    commands: tuple[tuple[TubeCommand, ...], ...]
    steps: tuple[JointPathCertificate, ...]
    terminal: tuple[TubeBox, ...]
    terminal_commands: tuple[TubeCommand, ...]
    terminal_proof_digest: str

    @model_validator(mode="after")
    def axes(self) -> "ClusterContinuation":
        if (
            len(self.commands) != len(self.steps)
            or len(self.initial) != len(self.terminal)
            or any(len(c) != len(self.initial) for c in self.commands)
        ):
            raise ValueError("continuation axes differ")
        if any(s.minute != self.start_minute + i for i, s in enumerate(self.steps)):
            raise ValueError("continuation clock differs")
        return self


class ClusterSafetyStep(SafetyValue):
    minute: int
    source: Literal["economic", "backup", "terminal", "reserve"]
    reason: str = ""
    before: tuple[TubeState, ...]
    after: tuple[TubeState, ...]
    commands: tuple[TubeCommand, ...]
    observed: tuple[ClusterForecast, ...]
    path: JointPathCertificate
    reserve: ReserveClusterCertificate | None = None
    activation: tuple[ReserveActivationCommand, ...] = ()


class ClusterReserveEvent(SafetyValue):
    """Acceptance binds a full call envelope to its pre-certified return."""

    number: int = Field(ge=1)
    start_minute: int = Field(ge=0)
    end_minute: int = Field(gt=0)
    certificate: ReserveClusterCertificate
    successor: ClusterContinuation


class ClusterSafetyEvidence(SafetyValue):
    domain: ClusterSafetyDomain
    initial_continuation: ClusterContinuation | None = None
    continuations: tuple[ClusterContinuation, ...] = ()
    steps: tuple[ClusterSafetyStep, ...] = ()
    reserve_events: tuple[ClusterReserveEvent, ...] = ()
    failure: str = ""
