"""解析的端口（SDD 0043 第三节）。"""

from __future__ import annotations

from typing import Protocol

from app.domain.fact_needs import DatedFact, FactKey
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

    async def fact_candidates(
        self,
        subject: str,
        app: str,
        key: FactKey,
        *,
        public_access: bool = False,
    ) -> list[tuple[Candidate, list[DatedFact]]]: ...

    async def add_resolution(
        self, resolution: Resolution, *, expected_revision: int
    ) -> Resolution:
        """Append only if the latest revision matches the snapshot (0 for new)."""
        ...

    async def resolution(
        self, resolution_id: str, revision: int | None = None
    ) -> Resolution | None: ...

    async def add_confirmation(self, confirmation: Confirmation) -> Confirmation: ...

    async def confirmation(self, confirmation_id: str) -> Confirmation | None: ...

    async def preferences(self, subject: str, consumer: str) -> list[Preference]: ...

    async def save_preference(self, preference: Preference) -> Preference: ...
