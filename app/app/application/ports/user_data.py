"""用户资料的端口：记录与对象（SDD 0042）。"""

from __future__ import annotations

from datetime import datetime
from typing import Protocol

from app.domain.user_data import Consent, Grant, Item, Owner, ReadEvent


class UserDataRepository(Protocol):
    async def add_item(self, item: Item) -> Item:
        """记一份资料。同一个分区里来源编号（买来、代采）重复的回已有的那份。"""
        ...

    async def get_item(self, item_id: str) -> Item | None: ...

    async def items(self, ids: list[str]) -> dict[str, Item]: ...

    async def list_items(self, owner: Owner) -> list[Item]:
        """这个分区里没删的资料，新的在前。"""
        ...

    async def bytes_used(self, owner: Owner) -> int: ...

    async def find_by_origin(self, owner: Owner, origin_ref: str) -> Item | None: ...

    async def delete_item(self, item_id: str, now: datetime) -> None:
        """同一个事务里核引用、记删除。

        还被有效的运行授权或没撤的同意依据引用就抛 409。
        """
        ...

    async def add_consent(self, consent: Consent) -> Consent: ...

    async def get_consent(self, consent_id: str) -> Consent | None: ...

    async def withdraw_consent(self, consent_id: str, now: datetime) -> None: ...

    async def add_grant(self, grant: Grant) -> Grant:
        """在同一个事务里锁住引到的资料再记授权：资料这时被删了就抛 404。"""
        ...

    async def get_grant(self, grant_id: str) -> Grant | None: ...

    async def revoke_grant(self, grant_id: str, now: datetime) -> None: ...

    async def record_read(self, event: ReadEvent) -> None: ...


class UserObjects(Protocol):
    """存资料内容的对象存储。键由领域层算（`object_key`），这里不解释。"""

    async def put(self, key: str, content: bytes, media_type: str) -> None: ...

    async def get(self, key: str) -> bytes: ...

    async def delete(self, key: str) -> None: ...

    async def copy(self, source_key: str, target_key: str) -> None: ...
