"""用户资料的公共件（SDD 0042 起，第四版 SDD 0043 沿用）。

v1 按「用户 × 应用」分区的资料、同意依据、运行授权已删（decisions ⑰），留下第四版
（`user_library.py`、`user_runs.py`、`user_resolution.py`、`user_facts.py`）共用的几样：
用户令牌认出的「谁、哪个应用代办」、拒绝的写法、收文件的检查、读格式。

资料一经上传就不改：内容变了就是另一版（另一个 sha256）。
"""

from __future__ import annotations

import io
import re
import zipfile
from dataclasses import dataclass
from typing import Literal

App = Literal["knowbench", "knowlab", "knowmart"]
APPS: tuple[App, ...] = ("knowbench", "knowlab", "knowmart")

MAX_ITEM_BYTES = 50 * 1024 * 1024
QUOTA_BYTES = 1024 * 1024 * 1024

# 扩展名 → （内容类型，文件头）；与 knowlab 原来收的几种一致（0027 决定 4）
KINDS: dict[str, tuple[str, bytes | None]] = {
    ".pdf": ("application/pdf", b"%PDF-"),
    ".xlsx": (
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        b"PK\x03\x04",
    ),
    ".xls": ("application/vnd.ms-excel", b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"),
    ".csv": ("text/csv; charset=utf-8", None),
    ".txt": ("text/plain; charset=utf-8", None),
    ".md": ("text/markdown; charset=utf-8", None),
}

_BAD_NAME = re.compile(r"[\x00-\x1f\x7f/\\]")
_SHEET = re.compile(rb'<sheet\b[^>]*\bname="([^"]{1,100})"')


class UserDataError(Exception):
    """拒绝。`code` 原样回给调用方，`status` 是 HTTP 状态码。"""

    def __init__(self, status: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message


def refused(code: str, message: str) -> UserDataError:
    return UserDataError(422, code, message)


def not_found(what: str = "item") -> UserDataError:
    # 不是自己的与没有的一样，都是 404
    return UserDataError(404, f"{what}_not_found", f"No such {what}")


def conflict(code: str, message: str) -> UserDataError:
    return UserDataError(409, code, message)


@dataclass(frozen=True)
class Owner:
    """用户令牌认出的人：用户（Casdoor `sub`）与代办的应用（`azp`）。"""

    subject: str
    app: App


@dataclass(frozen=True)
class FormatView:
    """只看格式：表格有哪几张表、头一行是什么；不给内容（孵化授权读的就是这个）。"""

    sheets: tuple[str, ...]
    header: str | None


def clean_name(name: str | None) -> str:
    value = (name or "").strip()
    if not value or len(value) > 200 or _BAD_NAME.search(value):
        raise refused("name_invalid", "file name is missing or invalid")
    return value


def kind_of(name: str, content: bytes) -> str:
    """按扩展名定内容类型，再核文件头；文本类要求是 UTF-8。返回内容类型。"""
    suffix = "." + name.rsplit(".", 1)[-1].lower() if "." in name else ""
    if suffix not in KINDS:
        raise refused(
            "type_not_allowed",
            "only PDF, Excel (xlsx/xls), CSV, text and Markdown are accepted",
        )
    media_type, magic = KINDS[suffix]
    if magic is not None:
        if not content.startswith(magic):
            raise refused("content_mismatch", "file content does not match its type")
    else:
        try:
            content.decode("utf-8")
        except UnicodeDecodeError:
            raise refused("not_utf8", "text files must be UTF-8") from None
    return media_type


def check_size(size: int, used: int) -> None:
    if size <= 0:
        raise refused("empty", "file is empty")
    if size > MAX_ITEM_BYTES:
        raise UserDataError(413, "too_large", "file is larger than 50 MB")
    if used + size > QUOTA_BYTES:
        raise UserDataError(413, "quota_exceeded", "storage quota of 1 GB is used up")


def format_view(media_type: str, content: bytes) -> FormatView:
    """按内容类型给格式：表格给表名（xlsx）或头一行（csv、txt、md）。

    PDF、xls 只给类型与大小（在版本上），这里两样都空。
    """
    if media_type.startswith(
        "application/vnd.openxmlformats-officedocument.spreadsheetml"
    ):
        try:
            with zipfile.ZipFile(io.BytesIO(content)) as book:
                workbook = book.read("xl/workbook.xml")
        except (zipfile.BadZipFile, KeyError):
            return FormatView(sheets=(), header=None)
        names = tuple(m.decode("utf-8", "replace") for m in _SHEET.findall(workbook))
        return FormatView(sheets=names[:50], header=None)
    if media_type.startswith("text/"):
        first = content.split(b"\n", 1)[0][:500].decode("utf-8", "replace").strip()
        return FormatView(sheets=(), header=first or None)
    return FormatView(sheets=(), header=None)
