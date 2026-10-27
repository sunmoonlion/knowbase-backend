"""运行：钉住的输入组合、运行授权（第四版）、产出区（SDD 0043 第 2.6、2.7、2.9 节）。

和 v1 授权（`user_data.Grant`）的不同：
- 不按应用分区：一份授权可以有多项，每项记钉住的版本（与读法）、权利依据、内容所有者；
  knowbase 签发时逐项核，取代「资料的主人 == 授权的主人」；
- 外层记过程、执行主体（哪个应用）、受益人；专家绑定的数据所有者是作者、受益人是
  请专家的人，留痕两人都记；
- 运行中追加数据新签一条（同一件事 `revision` 往上加），已签的不扩大；
- 平台过程的授权凭 `platform:` 依据，读的一方分运营（人）与机器，依据撤了授权一并撤。
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Literal

from app.domain.user_data import UserDataError, refused

Process = Literal[
    "test", "delegation", "session", "incubation", "platform", "processing"
]
PROCESSES: tuple[Process, ...] = (
    "test",
    "delegation",
    "session",
    "incubation",
    "platform",
    "processing",
)
# 用户本人在应用页面上签的；测试与平台过程由 knowlab 凭依据签；
# 加工由 knowbase 签给 knowfeed
USER_PROCESSES: tuple[Process, ...] = ("delegation", "session", "incubation")
SERVICE_PROCESSES: tuple[Process, ...] = ("test", "platform")

ReaderRole = Literal["app", "operator", "machine", "system"]
Scope = Literal["content", "format"]
GrantBasis = Literal["display", "frozen_binding", "platform", "processing", "reference"]
RenditionKind = Literal["text", "pages", "table", "index", "parse"]

# 时限：测试、委托 7 天，会话 24 小时，孵化 6 小时照旧；加工给 knowfeed 3 天
GRANT_TTL: dict[str, timedelta] = {
    "test": timedelta(days=7),
    "delegation": timedelta(days=7),
    "session": timedelta(hours=24),
    "incubation": timedelta(hours=6),
    "processing": timedelta(days=3),
}
# 平台过程的上限按过程配（拟：孵化 7 天、测试 14 天、交付审核 30 天；重跑同测试）
PLATFORM_TTL: dict[str, timedelta] = {
    "incubate": timedelta(days=7),
    "test": timedelta(days=14),
    "review": timedelta(days=30),
    "rerun": timedelta(days=14),
}
SCOPE_OF: dict[str, Scope] = {
    "test": "content",
    "delegation": "content",
    "session": "content",
    "incubation": "format",
    "platform": "content",
    "processing": "content",
}
# 每种过程认哪几种权利依据
BASES_OF: dict[str, tuple[GrantBasis, ...]] = {
    "test": ("display", "frozen_binding"),
    "delegation": ("display", "frozen_binding"),
    "session": ("display", "frozen_binding"),
    "incubation": ("display",),
    "platform": ("platform",),
    "processing": ("processing",),
}
# 展示依据要对得上执行的应用：委托与会话读 knowbench 栏，测试与孵化读 knowlab 栏
DISPLAY_APP_OF: dict[str, str] = {
    "test": "knowlab",
    "delegation": "knowbench",
    "session": "knowbench",
    "incubation": "knowlab",
}
MAX_PINS = 200
# 会话累计读到这么多份时界面提示一次，不挡
SESSION_HINT_AT = 50

_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_REF = re.compile(r"^[A-Za-z0-9._:@/-]{1,255}$")
_RENDITION_KINDS = ("text", "pages", "table", "index", "parse")


def check_ref(ref: str) -> str:
    if not _REF.fullmatch(ref):
        raise refused("ref_invalid", "ref is invalid")
    return ref


@dataclass(frozen=True)
class InputPin:
    """组合里的一项：内容版本与摘要；要读读法的再钉一个读法版本与摘要。"""

    material_id: str
    version_no: int
    sha256: str
    rendition_kind: str | None = None
    rendition_no: int | None = None
    rendition_sha256: str | None = None

    @property
    def key(self) -> tuple[str, int, str]:
        return (self.material_id, self.version_no, self.rendition_kind or "")

    def as_dict(self) -> dict[str, object]:
        return {
            "material_id": self.material_id,
            "version_no": self.version_no,
            "sha256": self.sha256,
            "rendition_kind": self.rendition_kind,
            "rendition_no": self.rendition_no,
            "rendition_sha256": self.rendition_sha256,
        }

    @staticmethod
    def from_dict(data: dict[str, object]) -> InputPin:
        return InputPin(
            material_id=str(data["material_id"]),
            version_no=int(data["version_no"]),  # type: ignore[arg-type]
            sha256=str(data["sha256"]),
            rendition_kind=data.get("rendition_kind"),  # type: ignore[arg-type]
            rendition_no=data.get("rendition_no"),  # type: ignore[arg-type]
            rendition_sha256=data.get("rendition_sha256"),  # type: ignore[arg-type]
        )


def check_pins(pins: tuple[InputPin, ...]) -> tuple[InputPin, ...]:
    """1 到 200 项、不重复；摘要的写法对；读法三样要么都给要么都不给。"""
    if not 1 <= len(pins) <= MAX_PINS:
        raise refused("pins_invalid", f"give 1 to {MAX_PINS} pins")
    if len({p.key for p in pins}) != len(pins):
        raise refused("pins_duplicate", "a pin is given twice")
    for pin in pins:
        if pin.version_no < 1 or not _SHA256.fullmatch(pin.sha256):
            raise refused("pins_invalid", "a pin is invalid")
        rendition = (pin.rendition_kind, pin.rendition_no, pin.rendition_sha256)
        if any(v is not None for v in rendition):
            if (
                pin.rendition_kind not in _RENDITION_KINDS
                or not isinstance(pin.rendition_no, int)
                or pin.rendition_no < 1
                or not _SHA256.fullmatch(pin.rendition_sha256 or "")
            ):
                raise refused("pins_invalid", "a rendition pin is invalid")
    return tuple(sorted(pins, key=lambda p: p.key))


def input_set_id(pins: tuple[InputPin, ...]) -> str:
    """组合的编号就是它的摘要：同样的几项永远是同一个编号，换一项就是另一个。"""
    canonical = json.dumps(
        [p.as_dict() for p in sorted(pins, key=lambda p: p.key)],
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class InputSet:
    id: str
    pins: tuple[InputPin, ...]
    created_at: datetime


@dataclass(frozen=True)
class GrantRequestItem:
    """申请的一项：钉哪一版（与读法）、凭什么依据。"""

    pin: InputPin
    basis: GrantBasis
    basis_ref: str


@dataclass(frozen=True)
class GrantItem:
    material_id: str
    version_no: int
    basis: GrantBasis
    basis_ref: str
    content_owner: str


@dataclass(frozen=True)
class RunGrant:
    id: str
    process: Process
    executor_app: str
    beneficiary: str
    issued_by: str
    ref: str
    revision: int
    reader_role: ReaderRole
    scope: Scope
    input_set_id: str
    items: tuple[GrantItem, ...]
    issued_at: datetime
    expires_at: datetime
    platform_use: str | None = None
    revoked_at: datetime | None = None
    revoke_reason: str | None = None

    def active(self, now: datetime) -> bool:
        return self.revoked_at is None and now < self.expires_at

    def item(self, material_id: str, version_no: int | None) -> GrantItem | None:
        """授权里这份资料的那一项；同一份钉了两版的要说是哪一版。"""
        found = [i for i in self.items if i.material_id == material_id]
        if version_no is not None:
            found = [i for i in found if i.version_no == version_no]
        if len(found) > 1:
            raise refused("version_required", "say which version to read")
        return found[0] if found else None


def grant_expiry(
    process: str,
    now: datetime,
    ttl_seconds: int | None,
    platform_kind: str | None = None,
) -> datetime:
    """申请方可以要更短；不给就用上限。"""
    limit = PLATFORM_TTL[platform_kind] if platform_kind else GRANT_TTL[process]
    if ttl_seconds is None:
        return now + limit
    if ttl_seconds <= 0:
        raise refused("ttl_invalid", "ttl must be positive")
    return now + min(timedelta(seconds=ttl_seconds), limit)


@dataclass(frozen=True)
class Output:
    """产出区的一份：替谁、哪个应用、哪件任务哪次运行写的。"""

    material_id: str
    owner: str
    caller: str
    task_ref: str
    run_ref: str
    name: str
    evidence: bool
    created_at: datetime


def denied(code: str, message: str) -> UserDataError:
    return UserDataError(403, code, message)
