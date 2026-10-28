"""用户资料的对象存储端口（SDD 0042 起，第四版沿用）。"""

from __future__ import annotations

from typing import Protocol


class UserObjects(Protocol):
    """存资料内容的对象存储。键由领域层算，这里不解释。"""

    async def put(self, key: str, content: bytes, media_type: str) -> None: ...

    async def get(self, key: str) -> bytes: ...

    async def delete(self, key: str) -> None: ...

    async def copy(self, source_key: str, target_key: str) -> None: ...
