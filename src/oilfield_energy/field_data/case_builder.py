"""从已审计现场资料构建优化算例前的失效关闭边界。"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from ..network_model import NetworkModelV2, assess_network_model
from .contracts import FieldAssetRegistry, LoadHistoryWorkbook, ShortCircuitWorkbook

if TYPE_CHECKING:
    from ..power_flow_comparison import FixedStateSnapshot
    from .snapshot_power_flow import FieldSnapshotRequest


class FieldDataNotReadyError(RuntimeError):
    pass


@dataclass(frozen=True)
class FieldCaseReadiness:
    ready: bool
    blockers: tuple[str, ...]
    warnings: tuple[str, ...]

    def require_ready(self) -> None:
        if not self.ready:
            raise FieldDataNotReadyError(
                "现场算例输入尚未就绪：" + "；".join(self.blockers)
            )


class FieldCaseBuilder:
    """校验资料完整性；未满足条件时绝不回退到合成拓扑。"""

    @staticmethod
    def build_fixed_snapshot(request: FieldSnapshotRequest, base: Path, *, demo: bool = False) -> FixedStateSnapshot:
        """独立现场时刻入口；不依赖优化所需的短路审计和全天时序资料。

        request 为 FieldSnapshotRequest，base 为其量测文件所在目录。
        build() 的 ProjectCase 时序构造仍是独立的待实现任务。
        """
        from .snapshot_power_flow import build_field_snapshot
        return build_field_snapshot(request, base, demo=demo)

    def __init__(
        self,
        registry: FieldAssetRegistry,
        short_circuit: ShortCircuitWorkbook,
        load_history: LoadHistoryWorkbook,
        *,
        network_model: NetworkModelV2 | None = None,
        operating_mode_id: str | None = None,
        engineering_units_confirmed: bool = False,
        station_mapping_confirmed: bool = False,
        pcc_semantics_confirmed: bool = False,
    ) -> None:
        self.registry = registry
        self.short_circuit = short_circuit
        self.load_history = load_history
        self.network_model = network_model
        self.operating_mode_id = operating_mode_id
        self.engineering_units_confirmed = engineering_units_confirmed
        self.station_mapping_confirmed = station_mapping_confirmed
        self.pcc_semantics_confirmed = pcc_semantics_confirmed

    def assess(self) -> FieldCaseReadiness:
        blockers: list[str] = []
        warnings: list[str] = []
        if self.network_model is None:
            blockers.append("缺少可验证的NetworkModelV2逐支路台账和运行方式")
        else:
            network = assess_network_model(
                self.network_model,
                self.operating_mode_id,
                require_field_approval=True,
            )
            blockers.extend(
                f"网络模型阻断:{reason}" for reason in network.blockers
            )
            warnings.extend(network.warnings)
        if not self.engineering_units_confirmed:
            blockers.append("历史量测单位与倍率未确认")
        if not self.station_mapping_confirmed:
            blockers.append("优化资源与实际母线/馈线/场站映射未确认")
        if not self.pcc_semantics_confirmed:
            blockers.append("PCC/考核点位置、正负方向和功率归属未确认")
        if not self.short_circuit.quality.valid:
            blockers.append("母线短路工作簿存在阻断级数据问题")
        if not self.load_history.quality.valid:
            blockers.append("线路负荷历史存在阻断级数据问题")
        if self.registry.unresolved_requirements:
            warnings.extend(self.registry.unresolved_requirements)
        return FieldCaseReadiness(not blockers, tuple(blockers), tuple(warnings))

    def build(self):
        """预留真实 ``ProjectCase`` 构建入口；资料不完备时明确失败。"""
        readiness = self.assess()
        readiness.require_ready()
        raise NotImplementedError(
            "现场资料已通过前置检查，但逐母线时序映射器尚未配置。"
        )


__all__ = ["FieldCaseBuilder", "FieldCaseReadiness", "FieldDataNotReadyError"]
