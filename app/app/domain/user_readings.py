"""正文、按页文字两种读法的内容（SDD 0043 第五节，数据拓扑第 16.2 节）。

两种都存在 knowbase 的 PostgreSQL：正文每个读法版本一行（标题、发布日期、正文），
按页文字每个读法版本每页一行。原件仍在对象存储。

读法版本的摘要按规范化后的内容算（同表格读法的事实批，0047 第 4.2 节）：钉住核的就是它，
写回时请求体怎么排版不影响摘要。

knowfeed 写回的格式（`user-data-supply`）：
- 正文：请求体是 UTF-8 文字；标题、发布日期是可选的查询参数；
- 按页文字：请求体是 JSON 数组 `[{"page": 1, "text": "…"}, …]`，页码从 1 起、不重复，
  一页没有字（扫描版的页）就是空串。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import date

from app.domain.user_data import refused

# 一份读法最多多少页、页码最大多少；knowfeed 一份 PDF 最多读 1500 页，这里留余量
MAX_PAGES = 10_000
MAX_PAGE_NO = 100_000
MAX_TITLE_CHARS = 500


@dataclass(frozen=True)
class Page:
    number: int
    text: str


@dataclass(frozen=True)
class TextReading:
    body: str
    title: str | None = None
    published_on: date | None = None


def _clean(text: str) -> tuple[str, int]:
    """PostgreSQL 的文字列存不了 NUL：去掉，去了几个照实记在读法上。"""
    removed = text.count("\x00")
    return (text.replace("\x00", "") if removed else text), removed


def parse_text(
    content: bytes, title: str | None = None, published_on: date | None = None
) -> tuple[TextReading, int]:
    """正文写回：返回正文与去掉了几个 NUL。"""
    try:
        decoded = content.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise refused("text_invalid", "text is not UTF-8") from exc
    body, removed = _clean(decoded)
    if not body.strip():
        raise refused("empty", "rendition is empty")
    if title is not None:
        title, dropped = _clean(title.strip())
        removed += dropped
        if len(title) > MAX_TITLE_CHARS:
            raise refused("title_invalid", f"title is at most {MAX_TITLE_CHARS}")
    return TextReading(body=body, title=title or None, published_on=published_on), (
        removed
    )


def parse_pages(content: bytes) -> tuple[tuple[Page, ...], int]:
    """按页文字写回：返回按页码排好的页与去掉了几个 NUL。"""
    try:
        given = json.loads(content.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise refused("pages_invalid", "pages is a JSON array") from exc
    if not isinstance(given, list) or not 1 <= len(given) <= MAX_PAGES:
        raise refused("pages_invalid", f"give 1 to {MAX_PAGES} pages")
    pages: dict[int, Page] = {}
    removed = 0
    for entry in given:
        if not isinstance(entry, dict) or set(entry) != {"page", "text"}:
            raise refused("pages_invalid", "each page is {page, text}")
        number, text = entry["page"], entry["text"]
        if (
            not isinstance(number, int)
            or isinstance(number, bool)
            or not 1 <= number <= MAX_PAGE_NO
            or not isinstance(text, str)
        ):
            raise refused("pages_invalid", "page is a number from 1, text a string")
        if number in pages:
            raise refused("pages_duplicate", f"page {number} is given twice")
        text, dropped = _clean(text)
        removed += dropped
        pages[number] = Page(number, text)
    return tuple(pages[n] for n in sorted(pages)), removed


def text_digest(reading: TextReading) -> str:
    return _digest(
        {
            "title": reading.title,
            "published_on": reading.published_on.isoformat()
            if reading.published_on
            else None,
            "text": reading.body,
        }
    )


def pages_digest(pages: tuple[Page, ...] | list[Page]) -> str:
    return _digest([{"page": p.number, "text": p.text} for p in pages])


def pages_bytes(pages: tuple[Page, ...] | list[Page]) -> bytes:
    """按页文字交给凭授权读的一方：和写回时同一个格式。"""
    return json.dumps(
        [{"page": p.number, "text": p.text} for p in pages], ensure_ascii=False
    ).encode("utf-8")


def _digest(document: object) -> str:
    canonical = json.dumps(
        document, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
