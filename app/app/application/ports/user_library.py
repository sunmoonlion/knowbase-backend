"""资料库的端口（SDD 0043 第二节）。对象存储沿用 `ports.user_data.UserObjects`。"""

from __future__ import annotations

from datetime import datetime
from typing import Protocol

from app.domain.user_library import (
    ContentVersion,
    Description,
    Holding,
    Material,
    ShelfEntry,
)


class UserLibraryRepository(Protocol):
    async def add_material(
        self, material: Material, version: ContentVersion, holding: Holding
    ) -> Material:
        """一个事务里记资料、第 1 版与所有者的使用权。"""
        ...

    async def add_version(self, version: ContentVersion) -> Material:
        """版本号必须正好是当前的下一个，否则 409 `version_conflict`。"""
        ...

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
