"""用户资料各组内部接口共用：领域的拒绝（`UserDataError`）照原样的码与状态回给调用方。"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from app.application.errors.exceptions import AppException
from app.domain.user_data import UserDataError


async def call(work: Callable[[], Any]) -> Any:
    try:
        return await work()
    except UserDataError as exc:
        raise AppException(
            code=exc.code, status_code=exc.status, msg=exc.message
        ) from None
