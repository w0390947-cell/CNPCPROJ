"""强类型、可审计且失效关闭的网络模型 V2。

本模块把网络台账与当前 ``MicrogridData.lines`` 的求解器兼容视图分开：

* V2 台账能够表达线路、变压器分接头、开关、并联补偿、运行方式和故障场景；
* 当前平衡正序径向 Branch Flow 求解器只接收它能够正确表达的子集；
* 不支持的相移、三相不平衡、网状或孤岛方式会明确阻断，绝不
  静默降级成原来的五节点线路模型。

项目方数据尚未闭合时，本模块只建立接口和校验规则，不补造任何现场参数。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from math import isclose, isfinite
from types import MappingProxyType
from typing import Iterable, Mapping, Protocol, Sequence


class NetworkModelError(ValueError):
    """网络台账、运行方式或求解器适配不满足要求。"""


class NetworkPhaseModel(str, Enum):
    BALANCED_POSITIVE_SEQUENCE = "balanced_positive_sequence"
    THREE_PHASE_UNBALANCED = "three_phase_unbalanced"


class NetworkBranchKind(str, Enum):
    LINE = "line"
    TRANSFORMER = "transformer"


class ShuntKind(str, Enum):
    CAPACITOR = "capacitor"
    REACTOR = "reactor"


@dataclass(frozen=True)
class NetworkDataProvenance:
    """网络数据包的来源和批准状态。

    ``synthetic`` 与 ``approved_for_field_use`` 互斥，避免合成台账被误标为现场
    台账。正式现场模型必须同时给出审批人和审批时间。
    """

    source: str
    dataset_version: str
    synthetic: bool = False
    approved_for_field_use: bool = False
    approved_by: str | None = None
    approved_at_utc: datetime | None = None

    def __post_init__(self) -> None:
        if not self.source.strip():
            raise NetworkModelError("network provenance source must not be empty")
        if not self.dataset_version.strip():
            raise NetworkModelError("network dataset version must not be empty")
        if self.synthetic and self.approved_for_field_use:
            raise NetworkModelError("synthetic network data cannot be approved for field use")
        if self.approved_for_field_use and (
            not self.approved_by or self.approved_at_utc is None
        ):
            raise NetworkModelError(
                "field-approved network data requires approved_by and approved_at_utc"
            )


@dataclass(frozen=True)
class NetworkBus:
    bus_id: str
    name: str
    nominal_voltage_kv: float | None
    provenance: str = ""

    def __post_init__(self) -> None:
        if not self.bus_id.strip():
            raise NetworkModelError("bus_id must not be empty")
        if not self.name.strip():
            raise NetworkModelError(f"bus {self.bus_id} name must not be empty")
        if self.nominal_voltage_kv is not None and (
            not isfinite(self.nominal_voltage_kv) or self.nominal_voltage_kv <= 0.0
        ):
            raise NetworkModelError(
                f"bus {self.bus_id} nominal voltage must be positive"
            )


@dataclass(frozen=True)
class TransformerTapChanger:
    minimum_position: int
    maximum_position: int
    neutral_position: int
    default_position: int
    step_percent: float

    def __post_init__(self) -> None:
        if not (
            self.minimum_position
            <= self.neutral_position
            <= self.maximum_position
        ):
            raise NetworkModelError("transformer neutral tap is outside tap range")
        if not (
            self.minimum_position
            <= self.default_position
            <= self.maximum_position
        ):
            raise NetworkModelError("transformer default tap is outside tap range")
        if not isfinite(self.step_percent) or self.step_percent < 0.0:
            raise NetworkModelError("transformer tap step must be nonnegative")
        if (
            self.minimum_position != self.maximum_position
            and self.step_percent == 0.0
        ):
            raise NetworkModelError("a movable tap changer requires a positive tap step")
        # 校验整个分接范围内不会形成零或负变比。
        for position in (self.minimum_position, self.maximum_position):
            if self.ratio_multiplier(position) <= 0.0:
                raise NetworkModelError("transformer tap range produces nonpositive ratio")

    def ratio_multiplier(self, position: int) -> float:
        if not self.minimum_position <= position <= self.maximum_position:
            raise NetworkModelError(f"transformer tap position {position} is out of range")
        return 1.0 + (position - self.neutral_position) * self.step_percent / 100.0


@dataclass(frozen=True)
class SeriesBranch:
    """线路或变压器串联支路。

    变压器 ``fixed_tap_ratio`` 是在两侧各自额定电压基准上的非额定标幺变比，
    不是高压侧/低压侧额定 kV 的直接比值。
    串联 R/X 位于声明方向的 to_bus 侧；反向解析时随变比折算，不保留原数值。
    """

    branch_id: str
    from_bus_id: str
    to_bus_id: str
    kind: NetworkBranchKind
    r_pu: float
    x_pu: float
    s_max_mva: float
    normally_in_service: bool = True
    fixed_tap_ratio: float = 1.0
    phase_shift_degrees: float = 0.0
    tap_changer: TransformerTapChanger | None = None
    provenance: str = ""

    def __post_init__(self) -> None:
        if not self.branch_id.strip():
            raise NetworkModelError("branch_id must not be empty")
        if not self.from_bus_id.strip() or not self.to_bus_id.strip():
            raise NetworkModelError(f"branch {self.branch_id} endpoints must not be empty")
        if self.from_bus_id == self.to_bus_id:
            raise NetworkModelError(f"branch {self.branch_id} cannot be a self-loop")
        if not isfinite(self.r_pu) or not isfinite(self.x_pu):
            raise NetworkModelError(f"branch {self.branch_id} impedance must be finite")
        if self.r_pu < 0.0 or self.x_pu < 0.0 or self.r_pu + self.x_pu <= 0.0:
            raise NetworkModelError(
                f"branch {self.branch_id} requires nonnegative, nonzero R/X"
            )
        if not isfinite(self.s_max_mva) or self.s_max_mva <= 0.0:
            raise NetworkModelError(f"branch {self.branch_id} capacity must be positive")
        if not isfinite(self.fixed_tap_ratio) or self.fixed_tap_ratio <= 0.0:
            raise NetworkModelError(f"branch {self.branch_id} tap ratio must be positive")
        if not isfinite(self.phase_shift_degrees):
            raise NetworkModelError(f"branch {self.branch_id} phase shift must be finite")
        if self.kind is NetworkBranchKind.LINE and (
            self.tap_changer is not None
            or not isclose(self.fixed_tap_ratio, 1.0, abs_tol=1e-12)
            or not isclose(self.phase_shift_degrees, 0.0, abs_tol=1e-12)
        ):
            raise NetworkModelError(
                f"line {self.branch_id} cannot carry transformer tap/phase-shift data"
            )

    def tap_ratio(self, position: int | None = None) -> float:
        if self.tap_changer is None:
            if position is not None:
                raise NetworkModelError(f"branch {self.branch_id} has no tap changer")
            return self.fixed_tap_ratio
        selected = self.tap_changer.default_position if position is None else position
        return self.fixed_tap_ratio * self.tap_changer.ratio_multiplier(selected)


@dataclass(frozen=True)
class NetworkSwitch:
    switch_id: str
    controlled_branch_id: str
    normally_closed: bool = True
    provenance: str = ""

    def __post_init__(self) -> None:
        if not self.switch_id.strip() or not self.controlled_branch_id.strip():
            raise NetworkModelError("switch id and controlled branch id must not be empty")


@dataclass(frozen=True)
class ShuntCompensator:
    """并联电容/电抗器台账。

    ``q_per_step_mvar`` 是母线电压为 1 pu 时每档的额定无功；实际注入按
    ``Q = Q_nominal * |V|²`` 计算。
    """

    shunt_id: str
    bus_id: str
    kind: ShuntKind
    q_per_step_mvar: float
    minimum_steps: int
    maximum_steps: int
    default_steps: int
    normally_in_service: bool = True
    provenance: str = ""

    def __post_init__(self) -> None:
        if not self.shunt_id.strip() or not self.bus_id.strip():
            raise NetworkModelError("shunt id and bus id must not be empty")
        if not isfinite(self.q_per_step_mvar) or self.q_per_step_mvar <= 0.0:
            raise NetworkModelError(f"shunt {self.shunt_id} step size must be positive")
        if not self.minimum_steps <= self.default_steps <= self.maximum_steps:
            raise NetworkModelError(f"shunt {self.shunt_id} default step is out of range")
        if self.minimum_steps < 0:
            raise NetworkModelError(f"shunt {self.shunt_id} minimum step cannot be negative")

    def reactive_power_mvar(self, steps: int) -> float:
        if not self.minimum_steps <= steps <= self.maximum_steps:
            raise NetworkModelError(f"shunt {self.shunt_id} step {steps} is out of range")
        sign = 1.0 if self.kind is ShuntKind.CAPACITOR else -1.0
        return sign * self.q_per_step_mvar * steps


@dataclass(frozen=True)
class SwitchState:
    switch_id: str
    closed: bool


@dataclass(frozen=True)
class BranchServiceState:
    branch_id: str
    in_service: bool


@dataclass(frozen=True)
class TransformerTapPosition:
    branch_id: str
    position: int


@dataclass(frozen=True)
class ShuntOperatingPoint:
    shunt_id: str
    in_service: bool
    steps: int


@dataclass(frozen=True)
class NetworkOperatingMode:
    mode_id: str
    description: str
    switch_states: tuple[SwitchState, ...] = ()
    branch_states: tuple[BranchServiceState, ...] = ()
    transformer_taps: tuple[TransformerTapPosition, ...] = ()
    shunt_operating_points: tuple[ShuntOperatingPoint, ...] = ()
    provenance: str = ""

    def __post_init__(self) -> None:
        if not self.mode_id.strip():
            raise NetworkModelError("operating mode id must not be empty")
        _require_unique((item.switch_id for item in self.switch_states), "mode switch")
        _require_unique((item.branch_id for item in self.branch_states), "mode branch")
        _require_unique((item.branch_id for item in self.transformer_taps), "mode tap")
        _require_unique((item.shunt_id for item in self.shunt_operating_points), "mode shunt")


@dataclass(frozen=True)
class NetworkContingency:
    contingency_id: str
    outaged_branch_ids: tuple[str, ...]
    description: str = ""
    enabled: bool = True
    provenance: str = ""

    def __post_init__(self) -> None:
        if not self.contingency_id.strip():
            raise NetworkModelError("contingency id must not be empty")
        if not self.outaged_branch_ids:
            raise NetworkModelError("contingency must remove at least one branch")
        _require_unique(self.outaged_branch_ids, "contingency branch")

    @property
    def order(self) -> int:
        return len(self.outaged_branch_ids)


@dataclass(frozen=True)
class NetworkModelV2:
    network_id: str
    base_mva: float
    pcc_bus_id: str
    buses: tuple[NetworkBus, ...]
    branches: tuple[SeriesBranch, ...]
    operating_modes: tuple[NetworkOperatingMode, ...]
    default_operating_mode_id: str
    provenance: NetworkDataProvenance
    phase_model: NetworkPhaseModel = NetworkPhaseModel.BALANCED_POSITIVE_SEQUENCE
    switches: tuple[NetworkSwitch, ...] = ()
    shunts: tuple[ShuntCompensator, ...] = ()
    contingencies: tuple[NetworkContingency, ...] = ()

    def __post_init__(self) -> None:
        if not self.network_id.strip():
            raise NetworkModelError("network_id must not be empty")
        if not isfinite(self.base_mva) or self.base_mva <= 0.0:
            raise NetworkModelError("network base_mva must be positive")
        if not self.buses:
            raise NetworkModelError("network requires at least one bus")
        if not self.operating_modes:
            raise NetworkModelError("network requires at least one operating mode")


@dataclass(frozen=True)
class ResolvedBranch:
    branch_id: str
    parent: str
    child: str
    kind: NetworkBranchKind
    r_pu: float
    x_pu: float
    s_max_mva: float
    tap_ratio: float
    phase_shift_degrees: float

    @property
    def name(self) -> str:
        """兼容现有 ``Line.name`` 的只读别名。"""
        return self.branch_id


@dataclass(frozen=True)
class ResolvedNetwork:
    network_id: str
    base_mva: float
    pcc_bus_id: str
    operating_mode_id: str
    contingency_id: str | None
    phase_model: NetworkPhaseModel
    buses: tuple[NetworkBus, ...]
    branches: tuple[ResolvedBranch, ...]
    shunt_q_nominal_mvar_by_bus: Mapping[str, float]

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "shunt_q_nominal_mvar_by_bus",
            MappingProxyType(dict(self.shunt_q_nominal_mvar_by_bus)),
        )

    @property
    def shunt_q_mvar_by_bus(self) -> Mapping[str, float]:
        """兼容别名；值是 1 pu 下的额定无功，不是任意电压下的实际无功。"""
        return self.shunt_q_nominal_mvar_by_bus


@dataclass(frozen=True)
class NetworkReadiness:
    structurally_valid: bool
    radial_connected: bool
    current_solver_compatible: bool
    field_approved: bool
    blockers: tuple[str, ...]
    warnings: tuple[str, ...]
    resolved_network: ResolvedNetwork | None = None
    active_branch_ids: tuple[str, ...] = ()
    reachable_bus_ids: tuple[str, ...] = ()
    disconnected_bus_ids: tuple[str, ...] = ()

    @property
    def ready_for_current_solver(self) -> bool:
        return (
            self.structurally_valid
            and self.radial_connected
            and self.current_solver_compatible
        )

    @property
    def ready_for_field_case(self) -> bool:
        return self.ready_for_current_solver and self.field_approved

    def require_current_solver_ready(self) -> ResolvedNetwork:
        if not self.ready_for_current_solver or self.resolved_network is None:
            raise NetworkModelError("network is not solver-ready: " + "; ".join(self.blockers))
        return self.resolved_network

    def require_field_ready(self) -> ResolvedNetwork:
        if not self.ready_for_field_case or self.resolved_network is None:
            raise NetworkModelError("network is not field-ready: " + "; ".join(self.blockers))
        return self.resolved_network


class _LegacyLine(Protocol):
    name: str
    parent: str
    child: str
    r_pu: float
    x_pu: float
    s_max_mva: float


def _require_unique(values: Iterable[str], label: str) -> None:
    seen: set[str] = set()
    duplicates: set[str] = set()
    for value in values:
        if value in seen:
            duplicates.add(value)
        seen.add(value)
    if duplicates:
        raise NetworkModelError(f"duplicate {label} ids: {sorted(duplicates)}")


def build_legacy_network_model_v2(
    *,
    network_id: str,
    buses: Sequence[str],
    pcc_bus_id: str,
    base_mva: float,
    lines: Sequence[_LegacyLine],
) -> NetworkModelV2:
    """将已有径向 ``Line`` 列表封装成明确标记的合成 V2 台账。"""
    return NetworkModelV2(
        network_id=network_id,
        base_mva=float(base_mva),
        pcc_bus_id=pcc_bus_id,
        buses=tuple(
            NetworkBus(bus_id=bus, name=bus, nominal_voltage_kv=None,
                       provenance="synthetic data.py")
            for bus in buses
        ),
        branches=tuple(
            SeriesBranch(
                branch_id=line.name,
                from_bus_id=line.parent,
                to_bus_id=line.child,
                kind=NetworkBranchKind.LINE,
                r_pu=float(line.r_pu),
                x_pu=float(line.x_pu),
                s_max_mva=float(line.s_max_mva),
                provenance="synthetic data.py",
            )
            for line in lines
        ),
        operating_modes=(
            NetworkOperatingMode(
                mode_id="synthetic-normal",
                description="合成五节点正常运行方式",
                provenance="synthetic data.py",
            ),
        ),
        default_operating_mode_id="synthetic-normal",
        provenance=NetworkDataProvenance(
            source="src/oilfield_energy/data.py",
            dataset_version="synthetic-v2-adapter-1",
            synthetic=True,
        ),
    )


def assess_network_model(
    model: NetworkModelV2,
    operating_mode_id: str | None = None,
    *,
    contingency_id: str | None = None,
    require_field_approval: bool = False,
) -> NetworkReadiness:
    """解析运行方式，并评估当前平衡正序径向求解器能否安全使用。"""
    blockers: list[str] = []
    warnings: list[str] = []

    bus_ids = [item.bus_id for item in model.buses]
    branch_ids = [item.branch_id for item in model.branches]
    switch_ids = [item.switch_id for item in model.switches]
    shunt_ids = [item.shunt_id for item in model.shunts]
    mode_ids = [item.mode_id for item in model.operating_modes]
    contingency_ids = [item.contingency_id for item in model.contingencies]
    for values, label in (
        (bus_ids, "BUS"),
        (branch_ids, "BRANCH"),
        (switch_ids, "SWITCH"),
        (shunt_ids, "SHUNT"),
        (mode_ids, "OPERATING_MODE"),
        (contingency_ids, "CONTINGENCY"),
    ):
        if len(values) != len(set(values)):
            blockers.append(f"DUPLICATE_{label}_ID")

    bus_set = set(bus_ids)
    branch_by_id = {item.branch_id: item for item in model.branches}
    switch_by_id = {item.switch_id: item for item in model.switches}
    shunt_by_id = {item.shunt_id: item for item in model.shunts}
    if model.pcc_bus_id not in bus_set:
        blockers.append("PCC_BUS_NOT_FOUND")
    if model.default_operating_mode_id not in set(mode_ids):
        blockers.append("DEFAULT_OPERATING_MODE_NOT_FOUND")
    for branch in model.branches:
        if branch.from_bus_id not in bus_set or branch.to_bus_id not in bus_set:
            blockers.append(f"BRANCH_ENDPOINT_NOT_FOUND:{branch.branch_id}")
    for switch in model.switches:
        if switch.controlled_branch_id not in branch_by_id:
            blockers.append(f"SWITCH_BRANCH_NOT_FOUND:{switch.switch_id}")
    for shunt in model.shunts:
        if shunt.bus_id not in bus_set:
            blockers.append(f"SHUNT_BUS_NOT_FOUND:{shunt.shunt_id}")

    selected_mode_id = operating_mode_id or model.default_operating_mode_id
    mode = next(
        (item for item in model.operating_modes if item.mode_id == selected_mode_id),
        None,
    )
    if mode is None:
        blockers.append(f"OPERATING_MODE_NOT_FOUND:{selected_mode_id}")

    contingency = None
    if contingency_id is not None:
        contingency = next(
            (item for item in model.contingencies if item.contingency_id == contingency_id),
            None,
        )
        if contingency is None:
            blockers.append(f"CONTINGENCY_NOT_FOUND:{contingency_id}")
        elif not contingency.enabled:
            blockers.append(f"CONTINGENCY_DISABLED:{contingency_id}")
        else:
            for branch_id in contingency.outaged_branch_ids:
                if branch_id not in branch_by_id:
                    blockers.append(f"CONTINGENCY_BRANCH_NOT_FOUND:{branch_id}")

    if mode is not None:
        for item in mode.switch_states:
            if item.switch_id not in switch_by_id:
                blockers.append(f"MODE_SWITCH_NOT_FOUND:{item.switch_id}")
        for item in mode.branch_states:
            if item.branch_id not in branch_by_id:
                blockers.append(f"MODE_BRANCH_NOT_FOUND:{item.branch_id}")
        for item in mode.transformer_taps:
            branch = branch_by_id.get(item.branch_id)
            if branch is None:
                blockers.append(f"MODE_TAP_BRANCH_NOT_FOUND:{item.branch_id}")
            elif branch.kind is not NetworkBranchKind.TRANSFORMER:
                blockers.append(f"MODE_TAP_ON_NON_TRANSFORMER:{item.branch_id}")
            elif branch.tap_changer is None:
                blockers.append(f"MODE_TAP_CHANGER_NOT_FOUND:{item.branch_id}")
            else:
                try:
                    branch.tap_ratio(item.position)
                except NetworkModelError:
                    blockers.append(f"MODE_TAP_OUT_OF_RANGE:{item.branch_id}")
        for item in mode.shunt_operating_points:
            shunt = shunt_by_id.get(item.shunt_id)
            if shunt is None:
                blockers.append(f"MODE_SHUNT_NOT_FOUND:{item.shunt_id}")
            else:
                try:
                    shunt.reactive_power_mvar(item.steps)
                except NetworkModelError:
                    blockers.append(f"MODE_SHUNT_STEP_OUT_OF_RANGE:{item.shunt_id}")

    structurally_valid = not blockers
    if not structurally_valid or mode is None:
        return NetworkReadiness(
            structurally_valid=False,
            radial_connected=False,
            current_solver_compatible=False,
            field_approved=False,
            blockers=tuple(dict.fromkeys(blockers)),
            warnings=tuple(warnings),
        )

    switch_overrides = {item.switch_id: item.closed for item in mode.switch_states}
    branch_overrides = {item.branch_id: item.in_service for item in mode.branch_states}
    tap_overrides = {item.branch_id: item.position for item in mode.transformer_taps}
    shunt_overrides = {item.shunt_id: item for item in mode.shunt_operating_points}
    outaged = set(contingency.outaged_branch_ids) if contingency is not None else set()
    switches_by_branch: dict[str, list[NetworkSwitch]] = {}
    for switch in model.switches:
        switches_by_branch.setdefault(switch.controlled_branch_id, []).append(switch)

    active: list[SeriesBranch] = []
    for branch in model.branches:
        enabled = branch_overrides.get(branch.branch_id, branch.normally_in_service)
        for switch in switches_by_branch.get(branch.branch_id, []):
            enabled = enabled and switch_overrides.get(
                switch.switch_id, switch.normally_closed
            )
        if branch.branch_id in outaged:
            enabled = False
        if enabled:
            active.append(branch)

    adjacency: dict[str, list[tuple[str, SeriesBranch]]] = {
        bus_id: [] for bus_id in bus_ids
    }
    for branch in active:
        adjacency[branch.from_bus_id].append((branch.to_bus_id, branch))
        adjacency[branch.to_bus_id].append((branch.from_bus_id, branch))

    visited: set[str] = set()
    oriented: list[ResolvedBranch] = []
    if model.pcc_bus_id in adjacency:
        visited.add(model.pcc_bus_id)
        queue = [model.pcc_bus_id]
        while queue:
            parent = queue.pop(0)
            for child, branch in adjacency[parent]:
                if child in visited:
                    continue
                visited.add(child)
                queue.append(child)
                position = tap_overrides.get(branch.branch_id)
                declared_ratio = branch.tap_ratio(position)
                declared_forward = parent == branch.from_bus_id
                oriented.append(
                    ResolvedBranch(
                        branch_id=branch.branch_id,
                        parent=parent,
                        child=child,
                        kind=branch.kind,
                        # 台账阻抗位于理想变压器的 to 侧。反向供电时，
                        # a'=1/a 且 z'=a²z，才保持同一个两端口导纳矩阵。
                        r_pu=branch.r_pu * (1.0 if declared_forward else declared_ratio ** 2),
                        x_pu=branch.x_pu * (1.0 if declared_forward else declared_ratio ** 2),
                        s_max_mva=branch.s_max_mva,
                        tap_ratio=(
                            declared_ratio if declared_forward else 1.0 / declared_ratio
                        ),
                        phase_shift_degrees=(
                            branch.phase_shift_degrees
                            if declared_forward
                            else -branch.phase_shift_degrees
                        ),
                    )
                )

    radial_connected = True
    if visited != bus_set:
        blockers.append("TOPOLOGY_ISLANDED")
        radial_connected = False
    if len(active) != max(0, len(bus_ids) - 1):
        blockers.append("TOPOLOGY_NOT_RADIAL")
        radial_connected = False

    shunt_q_by_bus = {bus_id: 0.0 for bus_id in bus_ids}
    for shunt in model.shunts:
        setting = shunt_overrides.get(shunt.shunt_id)
        in_service = (
            shunt.normally_in_service if setting is None else setting.in_service
        )
        steps = shunt.default_steps if setting is None else setting.steps
        if in_service:
            shunt_q_by_bus[shunt.bus_id] += shunt.reactive_power_mvar(steps)

    current_solver_compatible = radial_connected
    if model.phase_model is not NetworkPhaseModel.BALANCED_POSITIVE_SEQUENCE:
        blockers.append("THREE_PHASE_MODEL_NOT_IMPLEMENTED")
        current_solver_compatible = False
    if any(
        not isclose(item.phase_shift_degrees, 0.0, abs_tol=1e-12)
        for item in oriented
    ):
        blockers.append("PHASE_SHIFT_NOT_IMPLEMENTED")
        current_solver_compatible = False
    field_approved = (
        model.provenance.approved_for_field_use
        and not model.provenance.synthetic
        and all(bus.nominal_voltage_kv is not None for bus in model.buses)
        and all(bool(branch.provenance.strip()) for branch in model.branches)
    )
    if require_field_approval:
        if model.provenance.synthetic:
            blockers.append("SYNTHETIC_NETWORK_NOT_ALLOWED_FOR_FIELD_CASE")
        if not model.provenance.approved_for_field_use:
            blockers.append("NETWORK_DATASET_NOT_APPROVED")
        if any(bus.nominal_voltage_kv is None for bus in model.buses):
            blockers.append("BUS_NOMINAL_VOLTAGE_MISSING")
        if any(not branch.provenance.strip() for branch in model.branches):
            blockers.append("BRANCH_PROVENANCE_MISSING")

    resolved = None
    if radial_connected:
        resolved = ResolvedNetwork(
            network_id=model.network_id,
            base_mva=model.base_mva,
            pcc_bus_id=model.pcc_bus_id,
            operating_mode_id=selected_mode_id,
            contingency_id=contingency_id,
            phase_model=model.phase_model,
            buses=model.buses,
            branches=tuple(oriented),
            shunt_q_nominal_mvar_by_bus=shunt_q_by_bus,
        )
    return NetworkReadiness(
        structurally_valid=structurally_valid,
        radial_connected=radial_connected,
        current_solver_compatible=current_solver_compatible,
        field_approved=field_approved,
        blockers=tuple(dict.fromkeys(blockers)),
        warnings=tuple(warnings),
        resolved_network=resolved,
        active_branch_ids=tuple(branch.branch_id for branch in active),
        reachable_bus_ids=tuple(bus for bus in bus_ids if bus in visited),
        disconnected_bus_ids=tuple(bus for bus in bus_ids if bus not in visited),
    )


def assess_n_minus_one(
    model: NetworkModelV2,
    operating_mode_id: str | None = None,
) -> Mapping[str, NetworkReadiness]:
    """逐一解析已登记的单支路故障；不把孤岛场景伪装成可行。"""
    result = {
        item.contingency_id: assess_network_model(
            model,
            operating_mode_id,
            contingency_id=item.contingency_id,
            require_field_approval=False,
        )
        for item in model.contingencies
        if item.enabled and item.order == 1
    }
    return MappingProxyType(result)


def validate_legacy_network_alignment(microgrid: object) -> ResolvedNetwork:
    """保证旧 ``lines`` 视图与 V2 台账完全一致后才允许当前求解器使用。

    未挂 V2 的第三方/旧测试对象会被即时封装成明确的 synthetic V2；现场构建器
    不走该兼容分支，因此不会把旧模型误当成已批准真实台账。
    """
    model = getattr(microgrid, "network_model_v2", None)
    if model is None:
        model = build_legacy_network_model_v2(
            network_id=f"{getattr(microgrid, 'name')}-legacy-adapter",
            buses=tuple(getattr(microgrid, "buses")),
            pcc_bus_id=getattr(microgrid, "pcc_bus"),
            base_mva=float(getattr(microgrid, "base_mva")),
            lines=tuple(getattr(microgrid, "lines")),
        )
        mode_id = None
    else:
        mode_id = getattr(microgrid, "network_operating_mode_id", None)
    readiness = assess_network_model(model, mode_id)
    resolved = readiness.require_current_solver_ready()

    if tuple(bus.bus_id for bus in resolved.buses) != tuple(getattr(microgrid, "buses")):
        raise NetworkModelError("legacy buses do not match resolved NetworkModelV2 buses")
    if resolved.pcc_bus_id != getattr(microgrid, "pcc_bus"):
        raise NetworkModelError("legacy PCC does not match NetworkModelV2 PCC")
    if not isclose(resolved.base_mva, float(getattr(microgrid, "base_mva")), abs_tol=1e-12):
        raise NetworkModelError("legacy base_mva does not match NetworkModelV2")

    legacy_lines = tuple(getattr(microgrid, "lines"))
    legacy_by_id = {line.name: line for line in legacy_lines}
    if len(legacy_by_id) != len(legacy_lines):
        raise NetworkModelError("legacy line ids must be unique")
    for branch in resolved.branches:
        legacy = legacy_by_id.get(branch.branch_id)
        if legacy is None:
            raise NetworkModelError(
                f"resolved NetworkModelV2 branch {branch.branch_id} is missing from legacy view"
            )
        declared = next(item for item in model.branches if item.branch_id == branch.branch_id)
        if (
            {legacy.parent, legacy.child} != {declared.from_bus_id, declared.to_bus_id}
            or not isclose(float(legacy.r_pu), declared.r_pu, abs_tol=1e-12)
            or not isclose(float(legacy.x_pu), declared.x_pu, abs_tol=1e-12)
            or not isclose(float(legacy.s_max_mva), declared.s_max_mva, abs_tol=1e-12)
        ):
            raise NetworkModelError(
                f"legacy branch {legacy.name} diverges from resolved NetworkModelV2"
            )
    return resolved


__all__ = [
    "BranchServiceState",
    "NetworkBranchKind",
    "NetworkBus",
    "NetworkContingency",
    "NetworkDataProvenance",
    "NetworkModelError",
    "NetworkModelV2",
    "NetworkOperatingMode",
    "NetworkPhaseModel",
    "NetworkReadiness",
    "NetworkSwitch",
    "ResolvedBranch",
    "ResolvedNetwork",
    "SeriesBranch",
    "ShuntCompensator",
    "ShuntKind",
    "ShuntOperatingPoint",
    "SwitchState",
    "TransformerTapChanger",
    "TransformerTapPosition",
    "assess_n_minus_one",
    "assess_network_model",
    "build_legacy_network_model_v2",
    "validate_legacy_network_alignment",
]
