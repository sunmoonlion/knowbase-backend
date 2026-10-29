"""资料库的端口（SDD 0043 第二节）。对象存储沿用 `ports.user_data.UserObjects`。"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import Protocol

from app.domain.user_library import (
    Account,
    ContentVersion,
    Description,
    Expert,
    Holding,
    LogEntry,
    Material,
    ShelfEntry,
    Use,
)


class UserLibraryRepository(Protocol):
    async def add_material(
        self,
        material: Material,
        version: ContentVersion,
        holding: Holding,
        *,
        uses: Sequence[Use] = (),
        logs: Sequence[LogEntry] = (),
    ) -> Material:
        """一个事务里记资料、第 1 版、所有者的使用权，连同上传时的同意依据与留痕。"""
        ...

    async def add_version(self, version: ContentVersion) -> Material:
        """版本号必须正好是当前的下一个，否则 409 `version_conflict`。"""
        ...

    async def find_public(
        self, channel: str, channel_doc_id: str
    ) -> Material | None: ...

    async def get_material(self, material_id: str) -> Material | None: ...

    async def versions(self, material_id: str) -> list[ContentVersion]: ...

    async def version(
        self, material_id: str, version_no: int
    ) -> ContentVersion | None: ...

    async def holdings_of(self, subject: str, material_id: str) -> list[Holding]: ...

    async def shelf(self, subject: str) -> list[ShelfEntry]: ...

    async def bytes_used(self, subject: str) -> int: ...

    async def set_description(
        self, material_id: str, description: Description, actor: str, at: datetime
    ) -> Material: ...

    async def set_license(
        self,
        material_id: str,
        license_: str,
        actor: str,
        at: datetime,
        attested: bool,
    ) -> Material: ...

    async def history(self, material_id: str) -> list[dict[str, object]]: ...

    async def record_read(
        self,
        *,
        owner: str,
        material_id: str,
        sha256: str,
        reader: str,
        operation: str,
        acting_app: str | None,
        at: datetime,
        grant_id: str | None = None,
    ) -> None: ...

    async def remove(self, material_id: str, subject: str, at: datetime) -> None: ...

    async def add_use(self, use: Use) -> Use:
        """同一份资料、同一种用途已有有效的就回已有的那条。"""
        ...

    async def withdraw_use(
        self, subject: str, material_id: str, use: str, at: datetime
    ) -> bool: ...

    async def withdraw_named(self, subject: str, use: str, at: datetime) -> int: ...

    async def named(self, subject: str, prefix: str) -> list[Use]: ...

    async def uses_of(self, subject: str, material_id: str) -> list[Use]: ...

    async def material_ids_with_use(self, subject: str, use: str) -> set[str]: ...

    async def live_uses(self, subject: str) -> dict[str, list[str]]:
        """这个用户每份资料有效的用途（按资料记的那几种）。"""
        ...

    async def usage_by_subject(self, limit: int) -> list[dict[str, object]]: ...

    async def description_stats(self) -> list[dict[str, object]]: ...

    async def find_materials(
        self,
        *,
        owner: str | None = None,
        material_id: str | None = None,
        disabled: bool = False,
        limit: int = 100,
    ) -> list[Material]: ...

    async def account(self, subject: str) -> Account: ...

    async def save_account(self, account: Account, at: datetime) -> Account: ...

    async def upsert_expert(self, expert: Expert) -> Expert:
        """专家换了所有者就 409 `expert_owner_changed`。"""
        ...

    async def experts_of(self, subject: str) -> list[Expert]: ...

    async def add_holding(self, holding: Holding) -> Holding:
        """同一人、同一份、同一个交易编号已有有效的就回已有的那条。"""
        ...
