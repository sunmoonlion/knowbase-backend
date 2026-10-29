"""用户资料的对象存储端口（SDD 0042 起，第四版沿用）。"""

from __future__ import annotations

from typing import Protocol


class UserObjects(Protocol):
    """存资料内容的对象存储。键由领域层算，这里不解释。"""

    async def put(self, key: str, content: bytes, media_type: str) -> None: ...

    async def get(self, key: str) -> bytes: ...

    async def delete(self, key: str) -> None: ...


class FirstPageReader(Protocol):
    """读一份文件首页的文字，给上传时识别描述用（SDD 0044 第 2.3 节）。

    读不出（扫描版、坏文件、不认的格式）就回空串，不报错：识别只是建议。
    """

    def __call__(self, media_type: str, content: bytes) -> str: ...
