"""孵化只读表轴：第一行、第一列的标签，不含表格数据区，也不求值公式。

沿用孵化契约的有界投影：50 张表、200 列、500 行、每个标签 200 字。
CSV 第一行视为表头，先按 UTF-8、读不通再按 GB18030（国内常见导出，账 186）；
xlsx 按工作簿关系定位工作表，不猜 sheet1.xml，单元格省略 `r` 时按出现顺序
记为下一列（OOXML 允许）。
旧 xls、PDF、正文文件没有可可靠提取的表轴，回空；坏表格拒绝而非伪装为空。
"""

from __future__ import annotations

import csv
import io
import posixpath
import zipfile
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Literal
from xml.etree import ElementTree as ET

from app.domain.user_data import refused

MAX_XML = 8 * 1024 * 1024
MAX_TOTAL_XML = 32 * 1024 * 1024
MAIN = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
REL = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"


@dataclass(frozen=True)
class TableFormat:
    name: str
    columns: tuple[str, ...] = ()
    rows: tuple[str, ...] = ()
    complete_header: bool = False


def column_status(
    tables: tuple[TableFormat, ...], sheet: str, column: str
) -> Literal["present", "missing", "unknown"]:
    matches = [table for table in tables if table.name == sheet]
    if len(matches) != 1:
        return "unknown"
    table = matches[0]
    if column in table.columns and (len(column) < 200 or table.complete_header):
        return "present"
    return "missing" if table.complete_header else "unknown"


def _label(value: str) -> str:
    return value.strip()[:200]


def _row_label(value: str) -> str:
    value = _label(value)
    # 第一列可能就是数值，不将它误作行标签。
    if not value or value.startswith("="):
        return ""
    try:
        Decimal(value.replace(",", ""))
    except InvalidOperation:
        return value
    return ""


def _csv_text(content: bytes) -> str:
    try:
        return content.decode("utf-8-sig")
    except UnicodeDecodeError:
        # GB18030 覆盖 GBK、GB2312；再不通就照旧当坏表拒绝
        return content.decode("gb18030")


def table_formats(
    media_type: str, content: bytes, header_sheet: str | None = None
) -> tuple[TableFormat, ...]:
    try:
        if media_type.split(";", 1)[0] == "text/csv":
            if header_sheet not in (None, ""):
                return ()
            records = csv.reader(io.StringIO(_csv_text(content)), strict=True)
            header = next(records, [])
            rows: list[str] = []
            for number, row in enumerate(() if header_sheet is not None else records):
                if number >= 500:
                    break
                if row and (label := _row_label(row[0])):
                    rows.append(label)
            return (
                TableFormat(
                    "",
                    tuple(_label(v) for v in header[:200]),
                    tuple(rows),
                    bool(header)
                    and len(header) <= 200
                    and all(0 < len(v.strip()) <= 200 for v in header),
                ),
            )
        if (
            media_type
            == "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        ):
            return _xlsx(content, header_sheet)
    except (
        ValueError,
        KeyError,
        UnicodeError,
        csv.Error,
        ET.ParseError,
        zipfile.BadZipFile,
        RuntimeError,
        OSError,
    ):
        raise refused(
            "format_unreadable", "table format is malformed or exceeds limits"
        ) from None
    return ()


def _xlsx(content: bytes, header_sheet: str | None) -> tuple[TableFormat, ...]:
    with zipfile.ZipFile(io.BytesIO(content)) as book:
        consumed = 0

        def xml(path: str) -> ET.Element:
            nonlocal consumed
            info = book.getinfo(path)
            consumed += info.file_size
            if info.file_size > MAX_XML or consumed > MAX_TOTAL_XML:
                raise ValueError("XML limit")
            data = book.read(info)
            if b"<!DOCTYPE" in data or b"<!ENTITY" in data:
                raise ValueError("XML declarations")
            return ET.fromstring(data)

        workbook = xml("xl/workbook.xml")
        links = xml("xl/_rels/workbook.xml.rels")
        targets = {
            link.get("Id"): link.get("Target", "")
            for link in links
            if link.get("TargetMode") != "External"
        }
        strings: list[str] = []
        if "xl/sharedStrings.xml" in book.namelist():
            strings = [
                "".join(t.text or "" for t in item.iter(f"{MAIN}t"))
                for item in xml("xl/sharedStrings.xml")
            ]

        def cell_value(cell: ET.Element) -> str:
            if cell.find(f"{MAIN}f") is not None:
                return ""  # 不使用公式及其缓存值
            kind = cell.get("t")
            if kind == "inlineStr":
                return "".join(t.text or "" for t in cell.iter(f"{MAIN}t"))
            value = cell.findtext(f"{MAIN}v", "")
            if kind == "s":
                index = int(value)
                if not 0 <= index < len(strings):
                    raise ValueError("shared string index")
                return strings[index]
            return value if kind in (None, "n", "str") else ""

        tables: list[TableFormat] = []
        for sheet in list(workbook.iter(f"{MAIN}sheet"))[:50]:
            if header_sheet is not None and sheet.get("name") != header_sheet:
                continue
            target = targets.get(sheet.get(f"{REL}id"))
            if not target:
                raise ValueError("missing sheet relationship")
            path = posixpath.normpath(posixpath.join("xl", target))
            if target.startswith("/"):
                path = target.lstrip("/")
            if not path.startswith("xl/") or "\\" in path:
                raise ValueError("invalid sheet path")
            document = xml(path)
            columns: list[str] = []
            rows: list[str] = []
            complete = True
            for index, row in enumerate(document.iter(f"{MAIN}row")):
                if (header_sheet is not None and index > 0) or index > 500:
                    break
                if index == 0 and row.get("r", "1") != "1":
                    complete = False
                    if header_sheet is not None:
                        break
                column = 0
                for cell in row.findall(f"{MAIN}c"):
                    ref = cell.get("r")
                    if ref is None:
                        column += 1  # 省略 r：紧接上一格的下一列
                    else:
                        letters = ref.rstrip("0123456789")
                        if not letters or not all("A" <= c <= "Z" for c in letters):
                            raise ValueError("invalid cell reference")
                        column = 0
                        for letter in letters:
                            column = column * 26 + ord(letter) - ord("A") + 1
                    if index == 0 and column > 200:
                        complete = False
                    if index == 0 and column <= 200:
                        value = cell_value(cell).strip()
                        complete = complete and 0 < len(value) <= 200
                        columns.extend([""] * max(0, column - len(columns)))
                        columns[column - 1] = _label(value)
                    elif index > 0 and column == 1:
                        label = _row_label(cell_value(cell))
                        if label:
                            rows.append(label)
            tables.append(
                TableFormat(
                    sheet.get("name", "")[:255],
                    tuple(columns),
                    tuple(rows),
                    complete and bool(columns) and all(columns),
                )
            )
        return tuple(tables)
