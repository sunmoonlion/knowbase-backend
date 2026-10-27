"""解析的端口（SDD 0043 第三节）。"""

from __future__ import annotations

from datetime import datetime
from typing import Protocol

from app.domain.user_resolution import (
    Candidate,
    Confirmation,
    DescriptionKey,
    Preference,
    Resolution,
)


class ResolutionRepository(Protocol):
    async def candidates(
        self,
        subject: str,
        app: str,
        key: DescriptionKey,
        *,
        public_access: bool = False,
    ) -> list[Candidate]: ...

    async def add_resolution(self, resolution: Resolution) -> Resolution: ...

    async def resolution(
        self, resolution_id: str, revision: int | None = None
    ) -> Resolution | None: ...

    async def add_confirmation(self, confirmation: Confirmation) -> Confirmation: ...

    async def confirmation(self, confirmation_id: str) -> Confirmation | None: ...

    async def live_confirmations(
        self, subject: str, now: datetime
    ) -> list[Confirmation]: ...

    async def preferences(
        self, subject: str, consumer: str, contract_version: str, params_hash: str
    ) -> list[Preference]: ...

    async def save_preference(self, preference: Preference) -> Preference: ...
