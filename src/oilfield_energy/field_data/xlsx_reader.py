"""不依赖 Excel/COM 的最小只读 OOXML 工作簿读取器。"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any
from xml.etree import ElementTree as ET
from zipfile import BadZipFile, ZipFile


_MAIN = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
_DOC_REL = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
_PKG_REL = "http://schemas.openxmlformats.org/package/2006/relationships"


@dataclass(frozen=True)
class RawWorksheet:
    name: str
    rows: tuple[dict[str, Any], ...]


def _tag(namespace: str, local_name: str) -> str:
    return f"{{{namespace}}}{local_name}"


def _resolve_workbook_target(target: str) -> str:
    normalized = target.replace("\\", "/")
    if normalized.startswith("/"):
        return normalized.lstrip("/")
    return str(PurePosixPath("xl") / normalized)


def _shared_strings(archive: ZipFile) -> tuple[str, ...]:
    if "xl/sharedStrings.xml" not in archive.namelist():
        return ()
    root = ET.fromstring(archive.read("xl/sharedStrings.xml"))
    return tuple(
        "".join(node.text or "" for node in item.iter(_tag(_MAIN, "t")))
        for item in root
    )


def _cell_value(cell: ET.Element, shared: tuple[str, ...]) -> Any:
    kind = cell.attrib.get("t")
    if kind == "inlineStr":
        return "".join(node.text or "" for node in cell.iter(_tag(_MAIN, "t")))
    value = cell.find(_tag(_MAIN, "v"))
    if value is None or value.text is None:
        return None
    raw = value.text
    if kind == "s":
        return shared[int(raw)]
    if kind == "b":
        return raw == "1"
    if kind in {"str", "e"}:
        return raw
    try:
        number = float(raw)
    except ValueError:
        return raw
    return int(number) if number.is_integer() else number


def read_xlsx(path: str | Path) -> tuple[RawWorksheet, ...]:
    """读取工作表缓存值；不求值公式，也不会修改源文件。"""
    source = Path(path)
    if not source.is_file():
        raise FileNotFoundError(source)
    try:
        archive = ZipFile(source)
    except BadZipFile as exc:
        raise ValueError(f"not a valid .xlsx workbook: {source}") from exc
    with archive:
        shared = _shared_strings(archive)
        relations_root = ET.fromstring(archive.read("xl/_rels/workbook.xml.rels"))
        relations = {
            node.attrib["Id"]: node.attrib["Target"]
            for node in relations_root.findall(_tag(_PKG_REL, "Relationship"))
        }
        workbook = ET.fromstring(archive.read("xl/workbook.xml"))
        sheets_element = workbook.find(_tag(_MAIN, "sheets"))
        if sheets_element is None:
            return ()
        result: list[RawWorksheet] = []
        for sheet in sheets_element:
            relation_id = sheet.attrib[_tag(_DOC_REL, "id")]
            target = _resolve_workbook_target(relations[relation_id])
            root = ET.fromstring(archive.read(target))
            rows: list[dict[str, Any]] = []
            for row in root.iter(_tag(_MAIN, "row")):
                values = {
                    cell.attrib["r"]: _cell_value(cell, shared)
                    for cell in row.findall(_tag(_MAIN, "c"))
                }
                rows.append(values)
            result.append(RawWorksheet(sheet.attrib["name"], tuple(rows)))
        return tuple(result)


__all__ = ["RawWorksheet", "read_xlsx"]
