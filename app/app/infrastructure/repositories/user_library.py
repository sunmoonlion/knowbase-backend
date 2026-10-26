"""资料库的记录（PostgreSQL，SDD 0043 第二节）。

资料就是 `user_data_item` 的一行：v1 的几栏（sha256、object_key、size_bytes、
media_type）缓存当前版本，v1 的读法照旧能用；内容版本、使用权、改动记录在各自的表里。
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from datetime import datetime

from sqlalchemy import and_, exists, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.domain.user_data import conflict, not_found
from app.domain.user_library import (
    ContentVersion,
    Description,
    Holding,
    Material,
    ShelfEntry,
)
from app.infrastructure.models.user_data import (
    UserDataHolding,
    UserDataItem,
    UserDataMaterialLog,
    UserDataRead,
    UserDataVersion,
)

ORIGIN_OF_BASIS = {
    "upload": "upload",
    "collect": "collect",
    "purchase": "purchase",
    "reference": "collect",
    "output": "upload",
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


class SqlUserLibraryRepository:
    def __init__(
        self, sessions: Callable[[], async_sessionmaker[AsyncSession]]
    ) -> None:
        self._sessions = sessions

    async def add_material(
        self, material: Material, version: ContentVersion, holding: Holding
    ) -> Material:
        """一个事务里记资料、第 1 版与所有者的使用权。"""
        async with self._sessions()() as session:
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
            )
            _apply_description(row, material.description)
            session.add(row)
            await session.flush()
            session.add(_version_row(version))
            session.add(_holding_row(holding))
            await session.flush()
            await session.refresh(row)
            entity = _material(row)
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
            await session.commit()


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
