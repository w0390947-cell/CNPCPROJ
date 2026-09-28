"""从项目方文字资料中建立可追溯的设备事实注册表。"""

from __future__ import annotations

from oilfield_energy.modules.resources.contracts import WindReactivePolicy

from .contracts import (
    AssetKind,
    ControlCapability,
    FieldAssetRegistry,
    FieldRegion,
    MeasurementDirection,
    RenewableAsset,
)

_SOURCE = "西交大-资料提供.doc"
_SHANCHENG_SOURCE = "山城微电网控制策略逻辑.pdf"


def build_project_asset_registry() -> FieldAssetRegistry:
    """返回资料中已能确认的最小台账；不补造接线或容量。"""
    general_wind = ControlCapability(
        active_controllable=True,
        reactive_controllable=True,
        q_abs_over_p_max=WindReactivePolicy.GENERAL.ratio,
        provenance=_SOURCE,
        note="资料给出的风机无功能力口径为有功的0–33.3%。",
    )
    shancheng_wind = ControlCapability(
        active_controllable=True,
        reactive_controllable=True,
        q_abs_over_p_max=WindReactivePolicy.SHANCHENG.ratio,
        provenance=_SHANCHENG_SOURCE,
    )
    pv_active_only = ControlCapability(
        active_controllable=True,
        reactive_controllable=False,
        provenance=_SOURCE,
        note="西交大-资料提供.pdf第9页明确光伏纯有功输出，Q=0。",
    )
    assets = (
        RenewableAsset("wind-xinghe", "杏河风电", "杏河", AssetKind.WIND, 30.0,
                       capability=general_wind, provenance=_SOURCE),
        RenewableAsset("wind-huaziping", "化子坪风电", "化子坪", AssetKind.WIND, 5.0,
                       capability=general_wind, provenance=_SOURCE),
        RenewableAsset("wind-pingqiao", "坪桥风电", "坪桥", AssetKind.WIND, 5.0,
                       capability=general_wind, provenance=_SOURCE),
        RenewableAsset("wind-yushu", "榆树风电", "榆树", AssetKind.WIND, 5.0,
                       capability=general_wind, provenance=_SOURCE),
        RenewableAsset("wind-shaji", "沙集风电", "沙集", AssetKind.WIND, 25.0,
                       capability=general_wind, provenance=_SOURCE),
        RenewableAsset("wind-xinzhai", "新寨风电", "新寨", AssetKind.WIND, 20.0,
                       capability=general_wind, provenance=_SOURCE),
        RenewableAsset("wind-shancheng", "山城风电（2×5MW）", "山城", AssetKind.WIND, 10.0,
                       capability=shancheng_wind, provenance=_SHANCHENG_SOURCE),
        RenewableAsset(
            "storage-shancheng", "山城储能", "山城", AssetKind.STORAGE, 2.5, 5.0,
            ControlCapability(True, False, provenance=_SHANCHENG_SOURCE,
                              note="设备级策略固定储能Q=0。"),
            _SHANCHENG_SOURCE,
        ),
        RenewableAsset(
            "svg-shancheng", "山城SVG", "山城", AssetKind.SVG, None,
            capability=ControlCapability(
                False, True, q_min_mvar=-1.8, q_max_mvar=1.8,
                provenance=_SHANCHENG_SOURCE,
                note="额定容量2Mvar；设备级调度范围按±1.8Mvar，本地目标+1.5Mvar。",
            ),
            provenance=f"{_SOURCE}; {_SHANCHENG_SOURCE}",
        ),
    )
    regions = tuple(
        FieldRegion(name.lower(), name, None, _SOURCE)
        for name in ("杏河", "化子坪", "坪桥", "榆树", "沙集", "新寨", "山城")
    )
    return FieldAssetRegistry(
        regions=regions,
        assets=assets,
        pcc_raw_direction=MeasurementDirection.BUS_TO_LINE_POSITIVE,
        pv_default_capability=pv_active_only,
        wind_default_capability=general_wind,
        unresolved_requirements=(
            "一次接线的母线—线路端点和运行方式尚未形成机器可读台账",
            "线路R/X、长度、热稳限值与基准值尚未逐条确认",
            "线路负荷工作簿各量测列的工程单位和倍率尚未书面确认",
            "光伏场站装机、接入母线、爬坡与最小出力尚未完整映射",
            "D5000点表、质量码、时标、ACK/echo和控制权限尚未提供",
            "考核点与各微网PCC边界、功率归属及集群约束尚未确认",
        ),
    )


__all__ = ["build_project_asset_registry"]
