"""运行的端口（SDD 0043 块 1c）：输入组合、运行授权、读法、产出区、删除事件。"""

from __future__ import annotations

from datetime import datetime
from typing import Protocol

from app.domain.user_facts import FactQuery, FactRow, RawFact, StandardFact
from app.domain.user_library import ContentVersion, Holding, Material, Rendition
from app.domain.user_readings import Page, TextReading
from app.domain.user_runs import InputSet, Output, RunGrant, Session


class UserRunRepository(Protocol):
    async def has_use(
        self, subject: str, material_id: str, use: str, version_no: int | None = None
    ) -> bool: ...

    async def use_scope(
        self, subject: str, material_id: str, use: str, version_no: int
    ) -> dict[str, object] | None: ...

    async def live_processing_uses(self, limit: int) -> list[tuple[str, str, int]]: ...

    async def renditions(self, version_id: str) -> list[Rendition]: ...

    async def rendition(
        self, version_id: str, kind: str, rendition_no: int
    ) -> Rendition | None: ...

    async def add_rendition(
        self,
        rendition: Rendition,
        *,
        text: TextReading | None = None,
        pages: tuple[Page, ...] = (),
    ) -> Rendition: ...

    async def reading_text(self, rendition_id: str) -> TextReading | None: ...

    async def reading_pages(
        self, rendition_id: str, first: int | None = None, last: int | None = None
    ) -> list[Page]: ...

    async def page_count(self, rendition_id: str) -> int: ...

    async def search_pages(
        self, rendition_ids: list[str], terms: list[str], limit: int
    ) -> list[tuple[str, Page]]: ...

    async def add_fact_batch(
        self,
        rendition: Rendition,
        material_id: str,
        version_no: int,
        subject_code: str | None,
        raw: list[RawFact],
        standard: list[StandardFact],
    ) -> Rendition: ...

    async def latest_table(self, version_id: str) -> Rendition | None: ...

    async def facts(
        self, query: FactQuery, *, rendition_ids: list[str] | None = None
    ) -> list[FactRow]: ...

    async def save_input_set(self, input_set: InputSet) -> InputSet: ...

    async def input_set(self, input_set_id: str) -> InputSet | None: ...

    async def add_grant(self, grant: RunGrant, *, appendable: bool) -> RunGrant:
        """同一件事排队取下一个修订号；别人的同名事 409 `ref_taken`。"""
        ...

    async def get_grant(self, grant_id: str) -> RunGrant | None: ...

    async def grant_ids(
        self,
        *,
        executor_app: str | None = None,
        ref: str | None = None,
        beneficiary: str | None = None,
        platform_use: str | None = None,
        material_id: str | None = None,
        active_at: datetime | None = None,
    ) -> list[str]: ...

    async def revoke(self, grant_ids: list[str], at: datetime, reason: str) -> int: ...

    async def record_read(
        self,
        *,
        owner: str,
        material_id: str,
        sha256: str,
        reader: str,
        operation: str,
        acting_app: str,
        at: datetime,
        grant_id: str,
        beneficiary: str,
        reader_role: str,
    ) -> None: ...

    async def reads_of(self, material_id: str) -> list[dict[str, object]]: ...

    async def add_session(self, session: Session) -> Session: ...

    async def get_session(self, session_id: str) -> Session | None: ...

    async def end_session(self, session_id: str, at: datetime) -> None: ...

    async def find_output(
        self, owner: str, caller: str, task_ref: str, run_ref: str, name: str
    ) -> tuple[Output, ContentVersion] | None: ...

    async def add_output(
        self,
        material: Material,
        version: ContentVersion,
        holding: Holding,
        output: Output,
    ) -> Material: ...

    async def output(self, material_id: str) -> Output | None: ...

    async def disable(self, material_id: str, at: datetime) -> bool: ...

    async def other_holders(self, material_id: str, owner: str) -> int: ...

    async def platform_summary(
        self, subject: str, use: str
    ) -> tuple[datetime | None, int]: ...

    async def frozen_used_by_others(self, author: str, use: str) -> bool: ...

    async def owned_material_ids(self, subject: str) -> list[str]: ...

    async def end_holdings(self, subject: str, at: datetime, reason: str) -> int: ...

    async def withdraw_all_uses(self, subject: str, at: datetime) -> int: ...

    async def mark_purged(self, material_id: str, at: datetime) -> None: ...

    async def add_deletion(
        self,
        *,
        deletion_id: str,
        subject: str,
        material_id: str | None,
        action: str,
        reason: str,
        actor: str,
        at: datetime,
        pending: list[str],
    ) -> None: ...

    async def deletions_of(self, subject: str) -> list[dict[str, object]]: ...
