"""资料库的记录（PostgreSQL，SDD 0043 第二节）。

资料就是 `user_data_item` 的一行：v1 时代的几栏（sha256、object_key、size_bytes、
media_type）缓存当前版本；内容版本、使用权、改动记录在各自的表里。
"""

from __future__ import annotations

import uuid
from collections.abc import Callable, Sequence
from datetime import datetime
from typing import cast

from sqlalchemy import and_, exists, func, or_, select, update
from sqlalchemy.engine import CursorResult
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.domain.user_data import conflict, not_found
from app.domain.user_library import (
    PUBLIC_OWNER,
    Account,
    ContentVersion,
    Description,
    Expert,
    Holding,
    LogEntry,
    Material,
    Rendition,
    ShelfEntry,
    Use,
)
from app.infrastructure.models.user_data import (
    UserDataAccount,
    UserDataDeletion,
    UserDataExpert,
    UserDataHolding,
    UserDataItem,
    UserDataMaterialLog,
    UserDataRead,
    UserDataRendition,
    UserDataUse,
    UserDataVersion,
)


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
        detail=dict(row.detail or {}),
        ready_at=row.ready_at,
    )


async def _renditions(session: AsyncSession, version_id: str) -> list[Rendition]:
    rows = await session.execute(
        select(UserDataRendition)
        .where(UserDataRendition.version_id == uuid.UUID(version_id))
        .order_by(UserDataRendition.kind, UserDataRendition.rendition_no)
    )
    return [_rendition(row) for row in rows.scalars()]


ORIGIN_OF_BASIS = {
    "upload": "upload",
    "collect": "collect",
    "purchase": "purchase",
    "reference": "collect",
    "output": "upload",
    "convert": "upload",
}


def _uuid(value: str) -> uuid.UUID | None:
    try:
        return uuid.UUID(value)
    except (ValueError, TypeError):
        return None


def _description(row: UserDataItem) -> Description:
    return Description(
        subject=row.subject_code,
        doc_type=row.doc_type,
        period_start=row.period_start,
        period_end=row.period_end,
        disclosed_on=row.disclosed_on,
        data_format=row.data_format,
        status=row.description_status,  # type: ignore[arg-type]
        vocab_version=row.vocab_version or 0,
        edition=row.doc_edition or "full",
        topics=tuple(row.topics or ()),
        industry=row.industry,
        language=row.language,
        attributes=dict(row.attributes or {}),
    )


def _material(row: UserDataItem) -> Material:
    return Material(
        id=str(row.id),
        owner=row.owner_subject,
        name=row.name,
        content_origin=row.content_origin,  # type: ignore[arg-type]
        license=row.license,  # type: ignore[arg-type]
        description=_description(row),
        current_version=row.current_version,
        created_at=row.created_at,
        deleted_at=row.deleted_at,
        disabled_at=row.disabled_at,
        legacy_app=row.app,
        provenance=dict(row.provenance) if row.provenance is not None else None,
        purged_at=row.purged_at,
    )


def _version(row: UserDataVersion) -> ContentVersion:
    return ContentVersion(
        id=str(row.id),
        material_id=str(row.item_id),
        version_no=row.version_no,
        sha256=row.sha256,
        size_bytes=row.size_bytes,
        media_type=row.media_type,
        object_key=row.object_key,
        created_at=row.created_at,
        created_by=row.created_by,
        ragflow_dataset_id=getattr(row, "ragflow_dataset_id", None),
        ragflow_document_id=getattr(row, "ragflow_document_id", None),
        ragflow_job_id=getattr(row, "ragflow_job_id", None),
        ragflow_status=getattr(row, "ragflow_status", None),
    )


def _holding(row: UserDataHolding) -> Holding:
    return Holding(
        id=str(row.id),
        subject=row.subject,
        material_id=str(row.item_id),
        basis=row.basis,  # type: ignore[arg-type]
        license=row.license,  # type: ignore[arg-type]
        created_at=row.created_at,
        version_no=row.version_no,
        ref=row.ref,
        ended_at=row.ended_at,
        end_reason=row.end_reason,
    )


def _apply_description(row: UserDataItem, description: Description) -> None:
    row.subject_code = description.subject
    row.doc_type = description.doc_type
    row.period_start = description.period_start
    row.period_end = description.period_end
    row.disclosed_on = description.disclosed_on
    row.data_format = description.data_format
    row.description_status = description.status
    row.vocab_version = description.vocab_version
    row.doc_edition = description.edition
    row.topics = list(description.topics)
    row.industry = description.industry
    row.language = description.language
    row.attributes = dict(description.attributes)


async def insert_material(
    session: AsyncSession, material: Material, version: ContentVersion, holding: Holding
) -> Material:
    """在调用方的事务里写资料、第 1 版与所有者的使用权（产出区写入也用它）。"""
    row = UserDataItem(
        id=uuid.UUID(material.id),
        owner_subject=material.owner,
        app=material.legacy_app,
        name=material.name,
        media_type=version.media_type,
        size_bytes=version.size_bytes,
        sha256=version.sha256,
        origin=ORIGIN_OF_BASIS[holding.basis],
        origin_ref=None,
        object_key=version.object_key,
        created_at=material.created_at,
        no_resale=False,
        content_origin=material.content_origin,
        license=material.license,
        current_version=1,
        provenance=material.provenance,
    )
    _apply_description(row, material.description)
    session.add(row)
    await session.flush()
    session.add(_version_row(version))
    session.add(_holding_row(holding))
    await session.flush()
    await session.refresh(row)
    return _material(row)


class SqlUserLibraryRepository:
    def __init__(
        self, sessions: Callable[[], async_sessionmaker[AsyncSession]]
    ) -> None:
        self._sessions = sessions

    async def add_material(
        self,
        material: Material,
        version: ContentVersion,
        holding: Holding,
        *,
        uses: Sequence[Use] = (),
        logs: Sequence[LogEntry] = (),
    ) -> Material:
        """一个事务里记资料、第 1 版、所有者的使用权，连同上传时的同意依据（账号默认
        展示、直达上传的来源应用）与留痕（识别、上传时选的许可档）。"""
        async with self._sessions()() as session:
            entity = await insert_material(session, material, version, holding)
            key = uuid.UUID(material.id)
            for use in uses:
                session.add(
                    UserDataUse(
                        id=uuid.UUID(use.id),
                        subject=use.subject,
                        item_id=key,
                        use=use.use,
                        scope=dict(use.scope),
                        version_no=use.version_no,
                        created_at=use.created_at,
                        created_via=use.created_via,
                    )
                )
            for log in logs:
                session.add(
                    UserDataMaterialLog(
                        item_id=key,
                        at=material.created_at,
                        actor=material.owner,
                        what=log.what,
                        before=dict(log.before),
                        after=dict(log.after),
                    )
                )
            await session.commit()
            return entity

    async def add_version(self, version: ContentVersion) -> Material:
        """记下一个内容版本：锁住资料，版本号必须正好是当前的下一个；缓存跟着换。"""
        key = uuid.UUID(version.material_id)
        async with self._sessions()() as session:
            row = await session.get(UserDataItem, key, with_for_update=True)
            if row is None or row.deleted_at is not None or row.disabled_at is not None:
                raise not_found()
            if version.version_no != row.current_version + 1:
                raise conflict("version_conflict", "another version was added first")
            session.add(_version_row(version))
            row.current_version = version.version_no
            row.sha256 = version.sha256
            row.size_bytes = version.size_bytes
            row.media_type = version.media_type
            row.object_key = version.object_key
            await session.flush()
            await session.refresh(row)
            entity = _material(row)
            await session.commit()
            return entity

    async def find_collected_by_beneficiary(
        self, subject: str, channel: str, channel_doc_id: str, item_ref: str
    ) -> tuple[Material, ContentVersion] | None:
        async with self._sessions()() as session:
            row = await session.scalar(
                select(UserDataItem).where(
                    UserDataItem.owner_subject == subject,
                    UserDataItem.content_origin == "collected",
                    UserDataItem.provenance["channel"].astext == channel,
                    UserDataItem.provenance["channel_doc_id"].astext == channel_doc_id,
                    UserDataItem.provenance["item_ref"].astext == item_ref,
                )
            )
            if row is None:
                return None
            mat = _material(row)
            ver_row = await session.scalar(
                select(UserDataVersion).where(
                    UserDataVersion.item_id == row.id,
                    UserDataVersion.version_no == row.current_version,
                )
            )
            assert ver_row is not None
            return mat, _version(ver_row)

    async def find_public(self, channel: str, channel_doc_id: str) -> Material | None:
        """公开原件按「渠道 × 渠道里的编号」认，同一份只存一份。"""
        async with self._sessions()() as session:
            row = await session.scalar(
                select(UserDataItem).where(
                    UserDataItem.owner_subject == PUBLIC_OWNER,
                    UserDataItem.provenance["channel"].astext == channel,
                    UserDataItem.provenance["channel_doc_id"].astext == channel_doc_id,
                )
            )
            return _material(row) if row is not None else None

    async def renditions(self, version_id: str) -> list[Rendition]:
        async with self._sessions()() as session:
            return await _renditions(session, version_id)

    async def get_material(self, material_id: str) -> Material | None:
        key = _uuid(material_id)
        if key is None:
            return None
        async with self._sessions()() as session:
            row = await session.get(UserDataItem, key)
            return _material(row) if row is not None else None

    async def versions(self, material_id: str) -> list[ContentVersion]:
        key = _uuid(material_id)
        if key is None:
            return []
        async with self._sessions()() as session:
            rows = await session.execute(
                select(UserDataVersion)
                .where(UserDataVersion.item_id == key)
                .order_by(UserDataVersion.version_no)
            )
            return [_version(r) for r in rows.scalars()]

    async def version(self, material_id: str, version_no: int) -> ContentVersion | None:
        key = _uuid(material_id)
        if key is None:
            return None
        async with self._sessions()() as session:
            row = await session.scalar(
                select(UserDataVersion).where(
                    UserDataVersion.item_id == key,
                    UserDataVersion.version_no == version_no,
                )
            )
            return _version(row) if row is not None else None

    async def holdings_of(self, subject: str, material_id: str) -> list[Holding]:
        """这个用户对这份资料还有效的使用权。"""
        key = _uuid(material_id)
        if key is None:
            return []
        async with self._sessions()() as session:
            rows = await session.execute(
                select(UserDataHolding).where(
                    UserDataHolding.subject == subject,
                    UserDataHolding.item_id == key,
                    UserDataHolding.ended_at.is_(None),
                )
            )
            return [_holding(r) for r in rows.scalars()]

    async def shelf(self, subject: str) -> list[ShelfEntry]:
        """总栏：这个用户还有效的使用权，每条带资料与他能看到的那一版（新的在前）。

        所有者看当前版；买家看交易那一版。资料被禁用的不列。
        """
        async with self._sessions()() as session:
            rows = await session.execute(
                select(UserDataHolding, UserDataItem)
                .join(UserDataItem, UserDataItem.id == UserDataHolding.item_id)
                .where(
                    UserDataHolding.subject == subject,
                    UserDataHolding.ended_at.is_(None),
                    UserDataItem.disabled_at.is_(None),
                )
                .order_by(UserDataHolding.created_at.desc())
            )
            pairs = [(_holding(h), _material(i)) for h, i in rows.all()]
            wanted = [
                (uuid.UUID(m.id), h.version_no or m.current_version) for h, m in pairs
            ]
            versions: dict[tuple[str, int], ContentVersion] = {}
            if wanted:
                found = await session.execute(
                    select(UserDataVersion).where(
                        or_(
                            *(
                                and_(
                                    UserDataVersion.item_id == item,
                                    UserDataVersion.version_no == number,
                                )
                                for item, number in wanted
                            )
                        )
                    )
                )
                for row in found.scalars():
                    versions[(str(row.item_id), row.version_no)] = _version(row)
            return [
                ShelfEntry(m, h, versions[(m.id, h.version_no or m.current_version)])
                for h, m in pairs
                if (m.id, h.version_no or m.current_version) in versions
            ]

    async def bytes_used(self, subject: str) -> int:
        """他名下的资料占的空间：所有版本都算；自己删了、但还有别人的使用权的，照算
        （SDD 0043 第 2.2 节：资料算所有者的空间，直到最后一条别人的使用权失效）。"""
        others = exists().where(
            UserDataHolding.item_id == UserDataItem.id,
            UserDataHolding.subject != UserDataItem.owner_subject,
            UserDataHolding.ended_at.is_(None),
        )
        async with self._sessions()() as session:
            total = await session.scalar(
                select(func.coalesce(func.sum(UserDataVersion.size_bytes), 0))
                .join(UserDataItem, UserDataItem.id == UserDataVersion.item_id)
                .where(
                    UserDataItem.owner_subject == subject,
                    or_(UserDataItem.deleted_at.is_(None), others),
                )
            )
            return int(total or 0)

    async def set_description(
        self,
        material_id: str,
        description: Description,
        actor: str,
        at: datetime,
    ) -> Material:
        key = uuid.UUID(material_id)
        async with self._sessions()() as session:
            row = await session.get(UserDataItem, key, with_for_update=True)
            if row is None or row.deleted_at is not None:
                raise not_found()
            before = _description(row).as_dict()
            _apply_description(row, description)
            session.add(
                UserDataMaterialLog(
                    item_id=key,
                    at=at,
                    actor=actor,
                    what="description",
                    before=before,
                    after=description.as_dict(),
                )
            )
            await session.flush()
            await session.refresh(row)
            entity = _material(row)
            await session.commit()
            return entity

    async def set_license(
        self,
        material_id: str,
        license_: str,
        actor: str,
        at: datetime,
        attested: bool,
    ) -> Material:
        key = uuid.UUID(material_id)
        async with self._sessions()() as session:
            row = await session.get(UserDataItem, key, with_for_update=True)
            if row is None or row.deleted_at is not None:
                raise not_found()
            before = row.license
            row.license = license_
            # 所有者自己的使用权跟着改；买家的按交易时的依据，不动（第三轮评审 L3-11）
            await session.execute(
                update(UserDataHolding)
                .where(
                    UserDataHolding.item_id == key,
                    UserDataHolding.subject == row.owner_subject,
                    UserDataHolding.basis != "purchase",
                    UserDataHolding.ended_at.is_(None),
                )
                .values(license=license_)
            )
            session.add(
                UserDataMaterialLog(
                    item_id=key,
                    at=at,
                    actor=actor,
                    what="license",
                    before={"license": before},
                    after={"license": license_, "attested": attested},
                )
            )
            await session.flush()
            await session.refresh(row)
            entity = _material(row)
            await session.commit()
            return entity

    async def history(self, material_id: str) -> list[dict[str, object]]:
        key = _uuid(material_id)
        if key is None:
            return []
        async with self._sessions()() as session:
            rows = await session.execute(
                select(UserDataMaterialLog)
                .where(UserDataMaterialLog.item_id == key)
                .order_by(UserDataMaterialLog.at)
            )
            return [
                {
                    "at": r.at,
                    "actor": r.actor,
                    "what": r.what,
                    "before": r.before,
                    "after": r.after,
                }
                for r in rows.scalars()
            ]

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
    ) -> None:
        """读数留痕：只记编号不记内容；app 记代办这次读的应用。"""
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
                    grant_id=uuid.UUID(grant_id) if grant_id else None,
                )
            )
            await session.commit()

    async def remove(self, material_id: str, subject: str, at: datetime) -> None:
        """所有者在栏里删：逻辑删，所有者的使用权结束；买家的使用权与内容都不动。"""
        key = uuid.UUID(material_id)
        async with self._sessions()() as session:
            row = await session.get(UserDataItem, key, with_for_update=True)
            if (
                row is None
                or row.deleted_at is not None
                or row.owner_subject != subject
            ):
                raise not_found()
            row.deleted_at = at
            await session.execute(
                update(UserDataHolding)
                .where(
                    UserDataHolding.item_id == key,
                    UserDataHolding.subject == subject,
                    UserDataHolding.basis != "purchase",
                    UserDataHolding.ended_at.is_(None),
                )
                .values(ended_at=at, end_reason="deleted")
            )
            # 删除事件写明对象与原因：撤所有者的关系，内容不删（0043 第 2.8 节）
            session.add(
                UserDataDeletion(
                    id=uuid.uuid4(),
                    subject=subject,
                    item_id=key,
                    action="relation",
                    reason="owner-removed",
                    actor=subject,
                    requested_at=at,
                    state="done",
                    pending=[],
                    done_at=at,
                )
            )
            await session.commit()

    # —— 同意依据（第四版） ——

    async def add_use(self, use: Use) -> Use:
        """记一条同意依据；同一份资料（或账号级）、同一种用途已有有效的就回已有的那条。"""
        async with self._sessions()() as session:
            item = (
                UserDataUse.item_id == uuid.UUID(use.material_id)
                if use.material_id is not None
                else UserDataUse.item_id.is_(None)
            )
            existing = await session.scalar(
                select(UserDataUse).where(
                    UserDataUse.subject == use.subject,
                    item,
                    UserDataUse.use == use.use,
                    UserDataUse.withdrawn_at.is_(None),
                )
            )
            if existing is not None:
                return _use(existing)
            session.add(
                UserDataUse(
                    id=uuid.UUID(use.id),
                    subject=use.subject,
                    item_id=uuid.UUID(use.material_id) if use.material_id else None,
                    use=use.use,
                    scope=dict(use.scope),
                    version_no=use.version_no,
                    created_at=use.created_at,
                    created_via=use.created_via,
                )
            )
            await session.commit()
            return use

    async def withdraw_use(
        self, subject: str, material_id: str, use: str, at: datetime
    ) -> bool:
        """撤这份资料的这种用途；没有有效的回 False。"""
        async with self._sessions()() as session:
            result = await session.execute(
                update(UserDataUse)
                .where(
                    UserDataUse.subject == subject,
                    UserDataUse.item_id == uuid.UUID(material_id),
                    UserDataUse.use == use,
                    UserDataUse.withdrawn_at.is_(None),
                )
                .values(withdrawn_at=at)
            )
            await session.commit()
            return bool(cast(CursorResult, result).rowcount)

    async def raise_frozen_license(
        self,
        subject: str,
        use: str,
        material_id: str,
        version_no: int,
        license_: str,
        at: datetime,
    ) -> Use | None:
        """把冻结的那一份记下的许可改成 license_（只往高改由服务核），留许可留痕。"""
        key = uuid.UUID(material_id)
        async with self._sessions()() as session:
            row = await session.scalar(
                select(UserDataUse)
                .where(
                    UserDataUse.subject == subject,
                    UserDataUse.item_id == key,
                    UserDataUse.use == use,
                    UserDataUse.version_no == version_no,
                    UserDataUse.withdrawn_at.is_(None),
                )
                .with_for_update()
            )
            if row is None:
                return None
            before = dict(row.scope or {})
            row.scope = {**before, "license": license_}
            session.add(
                UserDataMaterialLog(
                    item_id=key,
                    at=at,
                    actor=subject,
                    what="license",
                    before={"frozen": use, "license": before.get("license")},
                    after={"frozen": use, "license": license_, "attested": True},
                )
            )
            await session.commit()
            return _use(row)

    async def withdraw_named(self, subject: str, use: str, at: datetime) -> int:
        """撤这个用户这种用途的全部有效依据（平台过程每份一行、订阅账号级一行）。"""
        async with self._sessions()() as session:
            result = await session.execute(
                update(UserDataUse)
                .where(
                    UserDataUse.subject == subject,
                    UserDataUse.use == use,
                    UserDataUse.withdrawn_at.is_(None),
                )
                .values(withdrawn_at=at)
            )
            await session.commit()
            return int(cast(CursorResult, result).rowcount or 0)

    async def named(self, subject: str, prefix: str) -> list[Use]:
        """用途以 `prefix` 开头的有效依据，按时间。"""
        async with self._sessions()() as session:
            rows = await session.execute(
                select(UserDataUse)
                .where(
                    UserDataUse.subject == subject,
                    UserDataUse.use.startswith(prefix, autoescape=True),
                    UserDataUse.withdrawn_at.is_(None),
                )
                .order_by(UserDataUse.created_at, UserDataUse.use)
            )
            return [_use(r) for r in rows.scalars()]

    async def uses_of(self, subject: str, material_id: str) -> list[Use]:
        async with self._sessions()() as session:
            rows = await session.execute(
                select(UserDataUse)
                .where(
                    UserDataUse.subject == subject,
                    UserDataUse.item_id == uuid.UUID(material_id),
                    UserDataUse.withdrawn_at.is_(None),
                )
                .order_by(UserDataUse.created_at)
            )
            return [_use(r) for r in rows.scalars()]

    async def material_ids_with_use(self, subject: str, use: str) -> set[str]:
        async with self._sessions()() as session:
            rows = await session.execute(
                select(UserDataUse.item_id).where(
                    UserDataUse.subject == subject,
                    UserDataUse.use == use,
                    UserDataUse.withdrawn_at.is_(None),
                    UserDataUse.item_id.is_not(None),
                )
            )
            return {str(r) for r in rows.scalars()}

    async def live_uses(self, subject: str) -> dict[str, list[str]]:
        async with self._sessions()() as session:
            rows = await session.execute(
                select(UserDataUse.item_id, UserDataUse.use)
                .where(
                    UserDataUse.subject == subject,
                    UserDataUse.withdrawn_at.is_(None),
                    UserDataUse.item_id.is_not(None),
                )
                .order_by(UserDataUse.created_at, UserDataUse.use)
            )
            found: dict[str, list[str]] = {}
            for item_id, use in rows.all():
                found.setdefault(str(item_id), []).append(use)
            return found

    # —— 管理端（0044 第四节）：用量、词表、禁用 ——

    async def usage_by_subject(self, limit: int) -> list[dict[str, object]]:
        """各用户的空间用量，多的在前；算法同 `bytes_used`。公开原件不算谁的空间。"""
        others = exists().where(
            UserDataHolding.item_id == UserDataItem.id,
            UserDataHolding.subject != UserDataItem.owner_subject,
            UserDataHolding.ended_at.is_(None),
        )
        used = func.coalesce(func.sum(UserDataVersion.size_bytes), 0)
        async with self._sessions()() as session:
            rows = await session.execute(
                select(
                    UserDataItem.owner_subject,
                    used.label("used"),
                    func.count(func.distinct(UserDataItem.id)).label("materials"),
                )
                .join(UserDataVersion, UserDataVersion.item_id == UserDataItem.id)
                .where(
                    UserDataItem.owner_subject != PUBLIC_OWNER,
                    or_(UserDataItem.deleted_at.is_(None), others),
                )
                .group_by(UserDataItem.owner_subject)
                .order_by(used.desc(), UserDataItem.owner_subject)
                .limit(limit)
            )
            return [
                {"subject": subject, "bytes_used": int(b), "materials": int(n)}
                for subject, b, n in rows.all()
            ]

    async def description_stats(self) -> list[dict[str, object]]:
        """描述按词表版本、确认状态、细类数一数（没清除的）；旧词表的要重新确认。"""
        async with self._sessions()() as session:
            rows = await session.execute(
                select(
                    UserDataItem.vocab_version,
                    UserDataItem.description_status,
                    UserDataItem.doc_type,
                    func.count(),
                )
                .where(UserDataItem.purged_at.is_(None))
                .group_by(
                    UserDataItem.vocab_version,
                    UserDataItem.description_status,
                    UserDataItem.doc_type,
                )
            )
            return [
                {
                    "vocab_version": version,
                    "status": status,
                    "doc_type": doc_type,
                    "count": int(count),
                }
                for version, status, doc_type, count in rows.all()
            ]

    async def find_materials(
        self,
        *,
        owner: str | None = None,
        material_id: str | None = None,
        disabled: bool = False,
        limit: int = 100,
    ) -> list[Material]:
        """管理端找资料：按所有者、编号，或只看禁用了的；新的在前。"""
        query = select(UserDataItem).where(UserDataItem.purged_at.is_(None))
        if owner is not None:
            query = query.where(UserDataItem.owner_subject == owner)
        if material_id is not None:
            key = _uuid(material_id)
            if key is None:
                return []
            query = query.where(UserDataItem.id == key)
        if disabled:
            query = query.where(UserDataItem.disabled_at.is_not(None))
        async with self._sessions()() as session:
            rows = await session.execute(
                query.order_by(UserDataItem.created_at.desc()).limit(limit)
            )
            return [_material(r) for r in rows.scalars()]

    # —— 账号设置、名下专家 ——

    async def account(self, subject: str) -> Account:
        async with self._sessions()() as session:
            row = await session.get(UserDataAccount, subject)
            if row is None:
                return Account(subject=subject)
            return Account(
                subject=subject,
                default_display=row.default_display,
                default_display_seen_at=row.default_display_seen_at,
            )

    async def save_account(self, account: Account, at: datetime) -> Account:
        async with self._sessions()() as session:
            row = await session.get(UserDataAccount, account.subject)
            if row is None:
                row = UserDataAccount(subject=account.subject, updated_at=at)
                session.add(row)
            row.default_display = account.default_display
            row.default_display_seen_at = account.default_display_seen_at
            row.updated_at = at
            await session.commit()
            return account

    async def upsert_expert(self, expert: Expert) -> Expert:
        async with self._sessions()() as session:
            row = await session.get(UserDataExpert, expert.expert_id)
            if row is None:
                row = UserDataExpert(expert_id=expert.expert_id)
                session.add(row)
            elif row.owner_subject != expert.owner:
                raise conflict("expert_owner_changed", "the expert has another owner")
            row.owner_subject = expert.owner
            row.name = expert.name
            row.latest_version = expert.latest_version
            row.updated_at = expert.updated_at
            await session.commit()
            return expert

    async def experts_of(self, subject: str) -> list[Expert]:
        async with self._sessions()() as session:
            rows = await session.execute(
                select(UserDataExpert)
                .where(UserDataExpert.owner_subject == subject)
                .order_by(UserDataExpert.name)
            )
            return [
                Expert(
                    expert_id=r.expert_id,
                    owner=r.owner_subject,
                    name=r.name,
                    latest_version=r.latest_version,
                    updated_at=r.updated_at,
                )
                for r in rows.scalars()
            ]

    # —— 买家的使用权 ——

    async def add_holding(self, holding: Holding) -> Holding:
        """记一条使用权；同一人、同一份、同一个交易编号已有有效的就回已有的那条。"""
        async with self._sessions()() as session:
            if holding.ref is not None:
                existing = await session.scalar(
                    select(UserDataHolding).where(
                        UserDataHolding.subject == holding.subject,
                        UserDataHolding.item_id == uuid.UUID(holding.material_id),
                        UserDataHolding.ref == holding.ref,
                        UserDataHolding.ended_at.is_(None),
                    )
                )
                if existing is not None:
                    return _holding(existing)
            session.add(_holding_row(holding))
            await session.commit()
            return holding


def _use(row: UserDataUse) -> Use:
    return Use(
        id=str(row.id),
        subject=row.subject,
        use=row.use,
        created_at=row.created_at,
        created_via=row.created_via,
        material_id=str(row.item_id) if row.item_id else None,
        version_no=row.version_no,
        scope=dict(row.scope or {}),
        withdrawn_at=row.withdrawn_at,
    )


def _version_row(version: ContentVersion) -> UserDataVersion:
    return UserDataVersion(
        id=uuid.UUID(version.id),
        item_id=uuid.UUID(version.material_id),
        version_no=version.version_no,
        sha256=version.sha256,
        size_bytes=version.size_bytes,
        media_type=version.media_type,
        object_key=version.object_key,
        created_at=version.created_at,
        created_by=version.created_by,
    )


def _holding_row(holding: Holding) -> UserDataHolding:
    return UserDataHolding(
        id=uuid.UUID(holding.id),
        subject=holding.subject,
        item_id=uuid.UUID(holding.material_id),
        basis=holding.basis,
        license=holding.license,
        version_no=holding.version_no,
        ref=holding.ref,
        created_at=holding.created_at,
    )
