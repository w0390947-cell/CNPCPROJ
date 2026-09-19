"""现场资料审计结果的可追踪 JSON 导出。"""

from __future__ import annotations

from dataclasses import asdict, is_dataclass
from datetime import datetime
from enum import Enum
import json
from pathlib import Path
from typing import Any

from .case_builder import FieldCaseBuilder
from .project_sources import build_project_asset_registry
from .workbook_import import audit_line_load_workbook, import_short_circuit_workbook


def _json_default(value: Any) -> Any:
    if is_dataclass(value):
        return asdict(value)
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, datetime):
        return value.isoformat()
    raise TypeError(f"cannot serialize {type(value).__name__}")


def write_field_data_audit(
    output_path: str | Path,
    *,
    short_circuit_workbook: str | Path,
    line_load_workbook: str | Path,
) -> dict[str, Any]:
    """审计两份项目工作簿并写出摘要；源工作簿保持只读。"""
    short_circuit = import_short_circuit_workbook(short_circuit_workbook)
    load_history = audit_line_load_workbook(line_load_workbook)
    registry = build_project_asset_registry()
    readiness = FieldCaseBuilder(registry, short_circuit, load_history).assess()
    payload = {
        "status": "field_data_not_ready" if not readiness.ready else "ready_for_mapping",
        "notice": (
            "本报告只审计原始资料，不代表现场算例已经建立；"
            "未确认单位、方向、拓扑和映射时不执行工程量换算。"
        ),
        "asset_registry": registry,
        "short_circuit_workbook": {
            "source": short_circuit.source,
            "sheet_name": short_circuit.sheet_name,
            "record_count": len(short_circuit.records),
            "quality": short_circuit.quality,
        },
        "line_load_workbook": {
            "source": load_history.source,
            "sheets": load_history.sheets,
            "quality": load_history.quality,
            "raw_units_confirmed": load_history.raw_units_confirmed,
            "raw_direction_confirmed": load_history.raw_direction_confirmed,
            "metadata": load_history.metadata,
        },
        "readiness": readiness,
    }
    target = Path(output_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, default=_json_default),
        encoding="utf-8",
    )
    return payload


__all__ = ["write_field_data_audit"]
