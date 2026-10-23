"""用户资料：各应用知识库的云端真源（SDD 0042）。

knowbase 按「用户 × 应用」分区存用户自己的资料：knowbench 云端知识库、knowlab 知识库、
knowmart 知识库各是一个应用分区，相互独立，不互相读（数据拓扑第十六、十七节）。

- 本人：应用后端拿用户的登录令牌向 Casdoor 换一张给 knowbase 的令牌（RFC 8693），
  `sub` 是用户，`azp` 是代办的应用；只能动这个用户在这个应用分区里的资料。
- 运行授权：测试、委托、会话、孵化四种，某一次运行只能读选中的那几份，
  到期或撤销后就读不了。
- 同意依据：用户本人留下「这几份可用于某件事」（knowlab 测试凭它签运行授权）。
- 每次读都留痕：谁、凭哪条授权、读了哪一份，只记编号不记内容。

资料一经上传就不改：内容变了就是另一份（另一个编号、另一个 sha256）。
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Literal

App = Literal["knowbench", "knowlab", "knowmart"]
APPS: tuple[App, ...] = ("knowbench", "knowlab", "knowmart")
# 买来的、代采的只能落进干活用的两个知识库（knowmart 的是卖家自己上传的）
RECEIVING_APPS: tuple[App, ...] = ("knowbench", "knowlab")

Origin = Literal["upload", "collect", "purchase"]
GrantKind = Literal["test", "delegation", "session", "incubation"]
GRANT_KINDS: tuple[GrantKind, ...] = ("test", "delegation", "session", "incubation")
# 用户本人在页面上点出来的三种；测试由 knowlab 凭同意依据签
USER_GRANT_KINDS: tuple[GrantKind, ...] = ("delegation", "session", "incubation")
Scope = Literal["content", "format"]

MAX_ITEM_BYTES = 50 * 1024 * 1024
QUOTA_BYTES = 1024 * 1024 * 1024
MAX_GRANT_ITEMS = 64
# 运行授权最长多久（到点兜底失效，数据拓扑第 5.3 节第 7 条）
MAX_GRANT_TTL: dict[GrantKind, timedelta] = {
    "test": timedelta(hours=26),  # 测试时限加等人的最长时间
    "delegation": timedelta(hours=24),
    "session": timedelta(hours=12),
    "incubation": timedelta(hours=6),
}
# 读格式的授权只看得到格式；其余三种看内容
GRANT_SCOPE: dict[GrantKind, Scope] = {
    "test": "content",
    "delegation": "content",
    "session": "content",
    "incubation": "format",
}

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
_REF = re.compile(r"^[A-Za-z0-9._:@/-]{1,255}$")
_PURPOSE = re.compile(r"^[a-z][a-z0-9-]{1,79}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


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
    """资料的主人：用户（Casdoor `sub`）在哪个应用分区。"""

    subject: str
    app: App


@dataclass(frozen=True)
class Item:
    id: str
    owner: Owner
    name: str
    media_type: str
    size_bytes: int
    sha256: str
    origin: Origin
    origin_ref: str | None
    object_key: str
    created_at: datetime
    deleted_at: datetime | None = None


@dataclass(frozen=True)
class Pin:
    """一份资料钉在哪一版：编号加摘要，摘要对不上就不认。"""

    item_id: str
    sha256: str


@dataclass(frozen=True)
class Consent:
    id: str
    owner: Owner
    purpose: str
    ref: str
    pins: tuple[Pin, ...]
    created_at: datetime
    withdrawn_at: datetime | None = None


@dataclass(frozen=True)
class Grant:
    id: str
    kind: GrantKind
    owner: Owner
    reader_app: App
    ref: str
    pins: tuple[Pin, ...]
    scope: Scope
    issued_by: str
    issued_at: datetime
    expires_at: datetime
    consent_id: str | None = None
    revoked_at: datetime | None = None

    def active(self, now: datetime) -> bool:
        return self.revoked_at is None and now < self.expires_at

    def pin(self, item_id: str) -> Pin | None:
        return next((p for p in self.pins if p.item_id == item_id), None)


@dataclass(frozen=True)
class ReadEvent:
    """一次读的留痕：只记编号，不记内容（数据拓扑第 5.3 节第 9 条）。"""

    owner: Owner
    item_id: str
    sha256: str
    reader: str
    operation: Literal["owner-download", "content", "format", "purchase-copy"]
    grant_id: str | None = None
    at: datetime | None = None


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


def check_ref(ref: str, what: str = "ref") -> str:
    if not _REF.fullmatch(ref or ""):
        raise refused(f"{what}_invalid", f"{what} is invalid")
    return ref


def check_purpose(purpose: str) -> str:
    if not _PURPOSE.fullmatch(purpose or ""):
        raise refused("purpose_invalid", "purpose is invalid")
    return purpose


def check_pins(pins: tuple[Pin, ...]) -> tuple[Pin, ...]:
    if not pins:
        raise refused("pins_empty", "at least one item is required")
    if len(pins) > MAX_GRANT_ITEMS:
        raise refused("pins_too_many", f"at most {MAX_GRANT_ITEMS} items")
    ids = [p.item_id for p in pins]
    if len(set(ids)) != len(ids):
        raise refused("pins_duplicate", "an item is listed twice")
    for p in pins:
        if not _SHA256.fullmatch(p.sha256):
            raise refused("sha256_invalid", "sha256 is invalid")
    return pins


def pins_match(pins: tuple[Pin, ...], items: dict[str, Item]) -> None:
    """每一份都在、没删、摘要对得上；否则当没有这份（404）或钉错了（409）。"""
    for p in pins:
        item = items.get(p.item_id)
        if item is None or item.deleted_at is not None:
            raise not_found()
        if item.sha256 != p.sha256:
            raise conflict("sha256_mismatch", "the pinned version does not match")


def grant_expiry(kind: GrantKind, now: datetime, ttl_seconds: int | None) -> datetime:
    longest = MAX_GRANT_TTL[kind]
    if ttl_seconds is None:
        return now + longest
    if ttl_seconds <= 0:
        raise refused("ttl_invalid", "ttl must be positive")
    return now + min(timedelta(seconds=ttl_seconds), longest)


def object_key(prefix: str, owner: Owner, item_id: str) -> str:
    """对象键按用户、应用分开；用户编号做了摘要，键里不出现原值。"""
    digest = hashlib.sha256(owner.subject.encode("utf-8")).hexdigest()
    return f"{prefix.rstrip('/')}/{digest[:2]}/{digest}/{owner.app}/{item_id}"
