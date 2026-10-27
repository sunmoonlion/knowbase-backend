"""运行的记录：输入组合、运行授权、读法、产出区、删除事件（SDD 0043 块 1c）。"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from datetime import datetime

from sqlalchemy import exists, func, select, text, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.domain.chart_of_accounts import CHART, CHART_VERSION
from app.domain.user_data import conflict, not_found
from app.domain.user_facts import FactQuery, FactRow, RawFact, StandardFact
from app.domain.user_library import (
    PUBLIC_OWNER,
    ContentVersion,
    Holding,
    Material,
    Rendition,
)
from app.domain.user_runs import (
    GrantItem,
    InputPin,
    InputSet,
    Output,
    RunGrant,
    Session,
)
from app.infrastructure.models.user_data import (
    UserDataDeletion,
    UserDataFact,
    UserDataFactRaw,
    UserDataHolding,
    UserDataInputSet,
    UserDataItem,
    UserDataOutput,
    UserDataRead,
    UserDataRendition,
    UserDataRunGrant,
    UserDataRunGrantItem,
    UserDataSession,
    UserDataUse,
    UserDataVersion,
)
from app.infrastructure.repositories.user_library import _version, insert_material


def _rendition(row: UserDataRendition) -> Rendition:
    return Rendition(
        id=str(row.id),
        version_id=str(row.version_id),
        kind=row.kind,  # type: ignore[arg-type]
        rendition_no=row.rendition_no,
        processor=row.processor,
        processor_version=row.processor_version,
        status=row.status,  # type: ignore[arg-type]
        created_at=row.created_at,
        sha256=row.sha256,
        object_key=row.object_key,
        detail=dict(row.detail or {}),
        ready_at=row.ready_at,
    )


def _grant(row: UserDataRunGrant, items: list[UserDataRunGrantItem]) -> RunGrant:
    return RunGrant(
        id=str(row.id),
        process=row.process,  # type: ignore[arg-type]
        executor_app=row.executor_app,
        beneficiary=row.beneficiary,
        issued_by=row.issued_by,
        ref=row.ref,
        revision=row.revision,
        reader_role=row.reader_role,  # type: ignore[arg-type]
        scope=row.scope,  # type: ignore[arg-type]
        input_set_id=row.input_set_id,
        items=tuple(
            GrantItem(
                material_id=str(i.item_id),
                version_no=i.version_no,
                basis=i.basis,  # type: ignore[arg-type]
                basis_ref=i.basis_ref,
                content_owner=i.content_owner,
            )
            for i in sorted(items, key=lambda i: (str(i.item_id), i.version_no))
        ),
        issued_at=row.issued_at,
        expires_at=row.expires_at,
        platform_use=row.platform_use,
        revoked_at=row.revoked_at,
        revoke_reason=row.revoke_reason,
    )


def _output(row: UserDataOutput) -> Output:
    return Output(
        material_id=str(row.item_id),
        owner=row.owner_subject,
        caller=row.caller,
        task_ref=row.task_ref,
        run_ref=row.run_ref,
        name=row.name,
        evidence=row.evidence,
        created_at=row.created_at,
    )


class SqlUserRunRepository:
    def __init__(
        self, sessions: Callable[[], async_sessionmaker[AsyncSession]]
    ) -> None:
        self._sessions = sessions

    # —— 依据与读法 ——

    async def has_use(
        self, subject: str, material_id: str, use: str, version_no: int | None = None
    ) -> bool:
        """这个人对这份资料有没有这条有效的同意依据；给了版本号的，依据要不限版本或正是这一版。"""
        async with self._sessions()() as session:
            query = select(UserDataUse.id).where(
                UserDataUse.subject == subject,
                UserDataUse.item_id == uuid.UUID(material_id),
                UserDataUse.use == use,
                UserDataUse.withdrawn_at.is_(None),
            )
            if version_no is not None:
                query = query.where(
                    (UserDataUse.version_no.is_(None))
                    | (UserDataUse.version_no == version_no)
                )
            return await session.scalar(query.limit(1)) is not None

    async def use_scope(
        self, subject: str, material_id: str, use: str, version_no: int
    ) -> dict[str, object] | None:
        """这条有效依据记下的范围（冻结绑定记着交付那一刻的许可）；没有就是 None。"""
        async with self._sessions()() as session:
            row = await session.scalar(
                select(UserDataUse).where(
                    UserDataUse.subject == subject,
                    UserDataUse.item_id == uuid.UUID(material_id),
                    UserDataUse.use == use,
                    UserDataUse.version_no == version_no,
                    UserDataUse.withdrawn_at.is_(None),
                )
            )
            return dict(row.scope or {}) if row is not None else None

    async def live_processing_uses(self, limit: int) -> list[tuple[str, str, int]]:
        """还有效的加工依据（所有者、资料、版本），新的在前；knowfeed 来领。"""
        async with self._sessions()() as session:
            rows = await session.execute(
                select(UserDataUse.subject, UserDataUse.item_id, UserDataUse.version_no)
                .join(UserDataItem, UserDataItem.id == UserDataUse.item_id)
                .where(
                    UserDataUse.use.startswith("processing:", autoescape=True),
                    UserDataUse.withdrawn_at.is_(None),
                    UserDataItem.disabled_at.is_(None),
                    UserDataItem.purged_at.is_(None),
                )
                .order_by(UserDataUse.created_at.desc())
                .limit(limit)
            )
            return [(s, str(i), int(v)) for s, i, v in rows.all()]

    async def renditions(self, version_id: str) -> list[Rendition]:
        async with self._sessions()() as session:
            rows = await session.execute(
                select(UserDataRendition)
                .where(UserDataRendition.version_id == uuid.UUID(version_id))
                .order_by(UserDataRendition.kind, UserDataRendition.rendition_no)
            )
            return [_rendition(r) for r in rows.scalars()]

    async def rendition(
        self, version_id: str, kind: str, rendition_no: int
    ) -> Rendition | None:
        async with self._sessions()() as session:
            row = await session.scalar(
                select(UserDataRendition).where(
                    UserDataRendition.version_id == uuid.UUID(version_id),
                    UserDataRendition.kind == kind,
                    UserDataRendition.rendition_no == rendition_no,
                )
            )
            return _rendition(row) if row is not None else None

    async def add_rendition(self, rendition: Rendition) -> Rendition:
        """读法号取这一版这一种的下一个（锁住内容版本那一行排队）。"""
        async with self._sessions()() as session:
            version = await session.get(
                UserDataVersion, uuid.UUID(rendition.version_id), with_for_update=True
            )
            if version is None:
                raise not_found("version")
            latest = await session.scalar(
                select(func.max(UserDataRendition.rendition_no)).where(
                    UserDataRendition.version_id == version.id,
                    UserDataRendition.kind == rendition.kind,
                )
            )
            row = UserDataRendition(
                id=uuid.UUID(rendition.id),
                version_id=version.id,
                kind=rendition.kind,
                rendition_no=(latest or 0) + 1,
                processor=rendition.processor,
                processor_version=rendition.processor_version,
                status=rendition.status,
                sha256=rendition.sha256,
                object_key=rendition.object_key,
                detail=dict(rendition.detail),
                created_at=rendition.created_at,
                ready_at=rendition.ready_at,
            )
            session.add(row)
            await session.flush()
            entity = _rendition(row)
            await session.commit()
            return entity

    # —— 事实（抽好的表） ——

    async def add_fact_batch(
        self,
        rendition: Rendition,
        material_id: str,
        version_no: int,
        subject_code: str | None,
        raw: list[RawFact],
        standard: list[StandardFact],
    ) -> Rendition:
        """一个事务里记表格读法版本与它的这批事实行（读法号取下一个）。"""
        async with self._sessions()() as session:
            version = await session.get(
                UserDataVersion, uuid.UUID(rendition.version_id), with_for_update=True
            )
            if version is None:
                raise not_found("version")
            latest = await session.scalar(
                select(func.max(UserDataRendition.rendition_no)).where(
                    UserDataRendition.version_id == version.id,
                    UserDataRendition.kind == "table",
                )
            )
            row = UserDataRendition(
                id=uuid.UUID(rendition.id),
                version_id=version.id,
                kind="table",
                rendition_no=(latest or 0) + 1,
                processor=rendition.processor,
                processor_version=rendition.processor_version,
                status="ready",
                sha256=rendition.sha256,
                object_key=None,
                detail=dict(rendition.detail),
                created_at=rendition.created_at,
                ready_at=rendition.ready_at,
            )
            session.add(row)
            await session.flush()
            item = uuid.UUID(material_id)
            session.add_all(
                UserDataFactRaw(
                    id=uuid.uuid4(),
                    rendition_id=row.id,
                    item_id=item,
                    version_no=version_no,
                    ordinal=n,
                    label=r.label,
                    value=r.value,
                    unit=r.unit,
                    currency=r.currency,
                    period_end=r.period_end,
                    duration=r.duration,
                    consolidation=r.consolidation,
                    statement_label=r.statement_label,
                    page=r.page,
                    row_no=r.row_no,
                    sheet=r.sheet,
                    cell=r.cell,
                )
                for n, r in enumerate(raw)
            )
            session.add_all(
                UserDataFact(
                    id=uuid.uuid4(),
                    rendition_id=row.id,
                    item_id=item,
                    version_no=version_no,
                    subject_code=subject_code,
                    chart_version=CHART_VERSION,
                    code=f.code,
                    value=f.value,
                    currency=f.currency,
                    period_end=f.period_end,
                    duration=f.duration,
                    consolidation=f.consolidation,
                    derived_from=list(f.derived_from),
                    rule=f.rule,
                    page=f.page,
                )
                for f in standard
            )
            await session.flush()
            entity = _rendition(row)
            await session.commit()
            return entity

    async def latest_table(self, version_id: str) -> Rendition | None:
        async with self._sessions()() as session:
            row = await session.scalar(
                select(UserDataRendition)
                .where(
                    UserDataRendition.version_id == uuid.UUID(version_id),
                    UserDataRendition.kind == "table",
                    UserDataRendition.status == "ready",
                )
                .order_by(UserDataRendition.rendition_no.desc())
                .limit(1)
            )
            return _rendition(row) if row is not None else None

    async def facts(
        self, query: FactQuery, *, rendition_ids: list[str] | None = None
    ) -> list[FactRow]:
        """按读法版本查（钉住的那几份）；`rendition_ids` 为空时查公开原件层全市场：
        每份公开原件取当前版本最新就绪的表格读法。"""
        table = UserDataFact if query.layer == "standard" else UserDataFactRaw
        latest = (
            select(func.max(UserDataRendition.rendition_no))
            .where(
                UserDataRendition.version_id == UserDataVersion.id,
                UserDataRendition.kind == "table",
                UserDataRendition.status == "ready",
            )
            .correlate(UserDataVersion)
            .scalar_subquery()
        )
        stmt = (
            select(table, UserDataItem, UserDataRendition)
            .join(UserDataRendition, UserDataRendition.id == table.rendition_id)
            .join(UserDataVersion, UserDataVersion.id == UserDataRendition.version_id)
            .join(UserDataItem, UserDataItem.id == table.item_id)
        )
        if rendition_ids is not None:
            stmt = stmt.where(
                table.rendition_id.in_([uuid.UUID(r) for r in rendition_ids])
            )
        else:
            stmt = stmt.where(
                UserDataItem.owner_subject == PUBLIC_OWNER,
                UserDataItem.disabled_at.is_(None),
                UserDataItem.purged_at.is_(None),
                UserDataVersion.version_no == UserDataItem.current_version,
                UserDataRendition.rendition_no == latest,
            )
        if query.subjects:
            stmt = stmt.where(UserDataItem.subject_code.in_(query.subjects))
        if query.layer == "standard":
            if query.codes:
                stmt = stmt.where(UserDataFact.code.in_(query.codes))
        elif query.labels:
            stmt = stmt.where(UserDataFactRaw.label.in_(query.labels))
        if query.period_from is not None:
            stmt = stmt.where(table.period_end >= query.period_from)
        if query.period_to is not None:
            stmt = stmt.where(table.period_end <= query.period_to)
        if query.duration is not None:
            stmt = stmt.where(table.duration == query.duration)
        if query.consolidation is not None:
            stmt = stmt.where(table.consolidation == query.consolidation)
        if query.value_min is not None:
            stmt = stmt.where(table.value >= query.value_min)
        if query.value_max is not None:
            stmt = stmt.where(table.value <= query.value_max)
        if query.order == "value_desc":
            stmt = stmt.order_by(table.value.desc().nulls_last())
        elif query.order == "value_asc":
            stmt = stmt.order_by(table.value.asc().nulls_last())
        else:
            stmt = stmt.order_by(
                UserDataItem.subject_code, table.period_end.desc(), table.id
            )
        async with self._sessions()() as session:
            rows = await session.execute(stmt.limit(query.limit))
            out: list[FactRow] = []
            for fact, item, rendition in rows.all():
                standard = query.layer == "standard"
                out.append(
                    FactRow(
                        subject=item.subject_code,
                        code=fact.code if standard else None,
                        label=CHART[fact.code].name if standard else fact.label,
                        value=fact.value,
                        unit="元" if standard else fact.unit,
                        currency=fact.currency,
                        period_end=fact.period_end,
                        duration=fact.duration,
                        consolidation=fact.consolidation,
                        material_id=str(item.id),
                        material_name=item.name,
                        version_no=fact.version_no,
                        rendition_no=rendition.rendition_no,
                        page=fact.page,
                        layer=query.layer,
                    )
                )
            return out

    # —— 输入组合 ——

    async def save_input_set(self, input_set: InputSet) -> InputSet:
        """编号就是摘要：已有的就是同一个组合，不再写。"""
        async with self._sessions()() as session:
            await session.execute(
                insert(UserDataInputSet)
                .values(
                    id=input_set.id,
                    pins=[p.as_dict() for p in input_set.pins],
                    created_at=input_set.created_at,
                )
                .on_conflict_do_nothing(index_elements=["id"])
            )
            await session.commit()
        return input_set

    async def input_set(self, input_set_id: str) -> InputSet | None:
        async with self._sessions()() as session:
            row = await session.get(UserDataInputSet, input_set_id)
            if row is None:
                return None
            return InputSet(
                id=row.id,
                pins=tuple(InputPin.from_dict(p) for p in row.pins),
                created_at=row.created_at,
            )

    # —— 运行授权 ——

    async def add_grant(self, grant: RunGrant, *, appendable: bool) -> RunGrant:
        """同一件事（执行应用 + ref）排队取下一个修订号；不许追加的已有就 409。"""
        async with self._sessions()() as session:
            await session.execute(
                text("SELECT pg_advisory_xact_lock(hashtext(:key))"),
                {"key": f"user_data_run_grant:{grant.executor_app}:{grant.ref}"},
            )
            same = UserDataRunGrant.executor_app == grant.executor_app
            same = same & (UserDataRunGrant.ref == grant.ref)
            latest = await session.scalar(
                select(func.max(UserDataRunGrant.revision)).where(same)
            )
            others = await session.scalar(
                select(UserDataRunGrant.id)
                .where(same, UserDataRunGrant.beneficiary != grant.beneficiary)
                .limit(1)
            )
            if others is not None:
                raise conflict("ref_taken", "this ref belongs to another user's run")
            if latest and not appendable:
                raise conflict(
                    "append_not_allowed", "this run does not take more data midway"
                )
            row = UserDataRunGrant(
                id=uuid.UUID(grant.id),
                process=grant.process,
                executor_app=grant.executor_app,
                beneficiary=grant.beneficiary,
                issued_by=grant.issued_by,
                ref=grant.ref,
                revision=(latest or 0) + 1,
                reader_role=grant.reader_role,
                scope=grant.scope,
                input_set_id=grant.input_set_id,
                platform_use=grant.platform_use,
                issued_at=grant.issued_at,
                expires_at=grant.expires_at,
            )
            session.add(row)
            await session.flush()
            items = [
                UserDataRunGrantItem(
                    grant_id=row.id,
                    item_id=uuid.UUID(i.material_id),
                    version_no=i.version_no,
                    basis=i.basis,
                    basis_ref=i.basis_ref,
                    content_owner=i.content_owner,
                )
                for i in grant.items
            ]
            session.add_all(items)
            await session.flush()
            entity = _grant(row, items)
            await session.commit()
            return entity

    async def get_grant(self, grant_id: str) -> RunGrant | None:
        try:
            key = uuid.UUID(grant_id)
        except (ValueError, TypeError):
            return None
        async with self._sessions()() as session:
            row = await session.get(UserDataRunGrant, key)
            if row is None:
                return None
            items = await session.execute(
                select(UserDataRunGrantItem).where(UserDataRunGrantItem.grant_id == key)
            )
            return _grant(row, list(items.scalars()))

    async def grant_ids(
        self,
        *,
        executor_app: str | None = None,
        ref: str | None = None,
        beneficiary: str | None = None,
        platform_use: str | None = None,
        material_id: str | None = None,
        active_at: datetime | None = None,
    ) -> list[str]:
        async with self._sessions()() as session:
            query = select(UserDataRunGrant.id)
            if executor_app is not None:
                query = query.where(UserDataRunGrant.executor_app == executor_app)
            if ref is not None:
                query = query.where(UserDataRunGrant.ref == ref)
            if beneficiary is not None:
                query = query.where(UserDataRunGrant.beneficiary == beneficiary)
            if platform_use is not None:
                query = query.where(UserDataRunGrant.platform_use == platform_use)
            if material_id is not None:
                query = query.where(
                    exists().where(
                        UserDataRunGrantItem.grant_id == UserDataRunGrant.id,
                        UserDataRunGrantItem.item_id == uuid.UUID(material_id),
                    )
                )
            if active_at is not None:
                query = query.where(
                    UserDataRunGrant.revoked_at.is_(None),
                    UserDataRunGrant.expires_at > active_at,
                )
            rows = await session.execute(
                query.order_by(UserDataRunGrant.issued_at, UserDataRunGrant.revision)
            )
            return [str(r) for r in rows.scalars()]

    async def revoke(self, grant_ids: list[str], at: datetime, reason: str) -> int:
        """撤还没撤的；已撤的再撤一次照样成功（不改原来的时间与原因）。"""
        if not grant_ids:
            return 0
        async with self._sessions()() as session:
            result = await session.execute(
                update(UserDataRunGrant)
                .where(
                    UserDataRunGrant.id.in_([uuid.UUID(g) for g in grant_ids]),
                    UserDataRunGrant.revoked_at.is_(None),
                )
                .values(revoked_at=at, revoke_reason=reason)
            )
            await session.commit()
            return int(result.rowcount or 0)

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
    ) -> None:
        async with self._sessions()() as session:
            session.add(
                UserDataRead(
                    at=at,
                    owner_subject=owner,
                    app=acting_app,
                    item_id=uuid.UUID(material_id),
                    sha256=sha256,
                    reader=reader,
                    operation=operation,
                    grant_id=uuid.UUID(grant_id),
                    beneficiary=beneficiary,
                    reader_role=reader_role,
                )
            )
            await session.commit()

    async def reads_of(self, material_id: str) -> list[dict[str, object]]:
        """这份资料的读数留痕（谁、凭哪条授权、哪一版的摘要、什么时候），新的在前。"""
        async with self._sessions()() as session:
            rows = await session.execute(
                select(UserDataRead)
                .where(UserDataRead.item_id == uuid.UUID(material_id))
                .order_by(UserDataRead.at.desc())
                .limit(500)
            )
            return [
                {
                    "at": r.at,
                    "reader": r.reader,
                    "operation": r.operation,
                    "app": r.app,
                    "grant_id": str(r.grant_id) if r.grant_id else None,
                    "sha256": r.sha256,
                    "beneficiary": r.beneficiary,
                    "reader_role": r.reader_role,
                }
                for r in rows.scalars()
            ]

    # —— 会话 ——

    async def add_session(self, session: Session) -> Session:
        async with self._sessions()() as db:
            db.add(
                UserDataSession(
                    id=uuid.UUID(session.id),
                    subject=session.subject,
                    executor_app=session.executor_app,
                    scope_use=session.scope_use,
                    created_at=session.created_at,
                    expires_at=session.expires_at,
                )
            )
            await db.commit()
        return session

    async def get_session(self, session_id: str) -> Session | None:
        try:
            key = uuid.UUID(session_id)
        except (ValueError, TypeError):
            return None
        async with self._sessions()() as db:
            row = await db.get(UserDataSession, key)
            if row is None:
                return None
            return Session(
                id=str(row.id),
                subject=row.subject,
                executor_app=row.executor_app,
                scope_use=row.scope_use,
                created_at=row.created_at,
                expires_at=row.expires_at,
                ended_at=row.ended_at,
            )

    async def end_session(self, session_id: str, at: datetime) -> None:
        async with self._sessions()() as db:
            await db.execute(
                update(UserDataSession)
                .where(
                    UserDataSession.id == uuid.UUID(session_id),
                    UserDataSession.ended_at.is_(None),
                )
                .values(ended_at=at)
            )
            await db.commit()

    # —— 产出区 ——

    async def find_output(
        self, owner: str, caller: str, task_ref: str, run_ref: str, name: str
    ) -> tuple[Output, ContentVersion] | None:
        async with self._sessions()() as session:
            row = await session.scalar(
                select(UserDataOutput).where(
                    UserDataOutput.owner_subject == owner,
                    UserDataOutput.caller == caller,
                    UserDataOutput.task_ref == task_ref,
                    UserDataOutput.run_ref == run_ref,
                    UserDataOutput.name == name,
                )
            )
            if row is None:
                return None
            version = await session.scalar(
                select(UserDataVersion).where(
                    UserDataVersion.item_id == row.item_id,
                    UserDataVersion.version_no == 1,
                )
            )
            return _output(row), _version(version)

    async def add_output(
        self,
        material: Material,
        version: ContentVersion,
        holding: Holding,
        output: Output,
    ) -> Material:
        """一个事务里写资料、第 1 版、使用权与产出记录；同一次运行同名的撞了就 409。"""
        async with self._sessions()() as session:
            entity = await insert_material(session, material, version, holding)
            session.add(
                UserDataOutput(
                    item_id=uuid.UUID(material.id),
                    owner_subject=output.owner,
                    caller=output.caller,
                    task_ref=output.task_ref,
                    run_ref=output.run_ref,
                    name=output.name,
                    evidence=output.evidence,
                    created_at=output.created_at,
                )
            )
            try:
                await session.commit()
            except IntegrityError as exc:  # 并发写同一次运行的同名产出
                await session.rollback()
                if "uq_user_data_output_run" in str(exc):
                    raise conflict(
                        "output_exists", "this run already wrote it"
                    ) from None
                raise
            return entity

    async def output(self, material_id: str) -> Output | None:
        async with self._sessions()() as session:
            row = await session.get(UserDataOutput, uuid.UUID(material_id))
            return _output(row) if row is not None else None

    # —— 禁用与清除 ——

    async def disable(self, material_id: str, at: datetime) -> bool:
        async with self._sessions()() as session:
            result = await session.execute(
                update(UserDataItem)
                .where(
                    UserDataItem.id == uuid.UUID(material_id),
                    UserDataItem.disabled_at.is_(None),
                )
                .values(disabled_at=at)
            )
            await session.commit()
            return bool(result.rowcount)

    async def other_holders(self, material_id: str, owner: str) -> int:
        """除所有者以外还有几条有效的使用权（买家、引用）。"""
        async with self._sessions()() as session:
            return int(
                await session.scalar(
                    select(func.count()).where(
                        UserDataHolding.item_id == uuid.UUID(material_id),
                        UserDataHolding.subject != owner,
                        UserDataHolding.ended_at.is_(None),
                    )
                )
                or 0
            )

    async def owned_material_ids(self, subject: str) -> list[str]:
        async with self._sessions()() as session:
            rows = await session.execute(
                select(UserDataItem.id).where(
                    UserDataItem.owner_subject == subject,
                    UserDataItem.purged_at.is_(None),
                )
            )
            return [str(r) for r in rows.scalars()]

    async def end_holdings(self, subject: str, at: datetime, reason: str) -> int:
        async with self._sessions()() as session:
            result = await session.execute(
                update(UserDataHolding)
                .where(
                    UserDataHolding.subject == subject,
                    UserDataHolding.ended_at.is_(None),
                )
                .values(ended_at=at, end_reason=reason)
            )
            await session.commit()
            return int(result.rowcount or 0)

    async def withdraw_all_uses(self, subject: str, at: datetime) -> int:
        async with self._sessions()() as session:
            result = await session.execute(
                update(UserDataUse)
                .where(
                    UserDataUse.subject == subject, UserDataUse.withdrawn_at.is_(None)
                )
                .values(withdrawn_at=at)
            )
            await session.commit()
            return int(result.rowcount or 0)

    async def mark_purged(self, material_id: str, at: datetime) -> None:
        """清除：描述字段、名字清掉，标清除时间；内容与读法由服务先删掉对象。"""
        async with self._sessions()() as session:
            row = await session.get(
                UserDataItem, uuid.UUID(material_id), with_for_update=True
            )
            if row is None:
                raise not_found()
            row.purged_at = at
            row.deleted_at = row.deleted_at or at
            row.name = "(cleared)"
            row.subject_code = None
            row.doc_type = None
            row.period_start = None
            row.period_end = None
            row.disclosed_on = None
            row.provenance = None
            await session.execute(
                update(UserDataHolding)
                .where(
                    UserDataHolding.item_id == row.id,
                    UserDataHolding.ended_at.is_(None),
                )
                .values(ended_at=at, end_reason="purged")
            )
            await session.commit()

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
    ) -> None:
        async with self._sessions()() as session:
            session.add(
                UserDataDeletion(
                    id=uuid.UUID(deletion_id),
                    subject=subject,
                    item_id=uuid.UUID(material_id) if material_id else None,
                    action=action,
                    reason=reason,
                    actor=actor,
                    requested_at=at,
                    state="processing" if pending else "done",
                    pending=pending,
                    done_at=None if pending else at,
                )
            )
            await session.commit()

    async def deletions_of(self, subject: str) -> list[dict[str, object]]:
        async with self._sessions()() as session:
            rows = await session.execute(
                select(UserDataDeletion)
                .where(UserDataDeletion.subject == subject)
                .order_by(UserDataDeletion.requested_at)
            )
            return [
                {
                    "id": str(r.id),
                    "material_id": str(r.item_id) if r.item_id else None,
                    "action": r.action,
                    "reason": r.reason,
                    "state": r.state,
                    "pending": list(r.pending),
                }
                for r in rows.scalars()
            ]
