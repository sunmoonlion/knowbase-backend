"""用户资料的记录（PostgreSQL，SDD 0042）。"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from datetime import datetime

from sqlalchemy import exists, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.domain.user_data import (
    Consent,
    Grant,
    Item,
    Owner,
    Pin,
    ReadEvent,
    conflict,
    not_found,
)
from app.infrastructure.models.user_data import (
    UserDataConsent,
    UserDataConsentItem,
    UserDataGrant,
    UserDataGrantItem,
    UserDataItem,
    UserDataRead,
)


def _uuid(value: str) -> uuid.UUID | None:
    try:
        return uuid.UUID(value)
    except (ValueError, TypeError):
        return None


def _item(row: UserDataItem) -> Item:
    return Item(
        id=str(row.id),
        owner=Owner(row.owner_subject, row.app),  # type: ignore[arg-type]
        name=row.name,
        media_type=row.media_type,
        size_bytes=row.size_bytes,
        sha256=row.sha256,
        origin=row.origin,  # type: ignore[arg-type]
        origin_ref=row.origin_ref,
        object_key=row.object_key,
        created_at=row.created_at,
        deleted_at=row.deleted_at,
    )


class SqlUserDataRepository:
    def __init__(
        self, sessions: Callable[[], async_sessionmaker[AsyncSession]]
    ) -> None:
        self._sessions = sessions  # 取会话工厂的函数：数据库在应用启动后才初始化

    async def add_item(self, item: Item) -> Item:
        async with self._sessions()() as session:
            row = UserDataItem(
                id=uuid.UUID(item.id),
                owner_subject=item.owner.subject,
                app=item.owner.app,
                name=item.name,
                media_type=item.media_type,
                size_bytes=item.size_bytes,
                sha256=item.sha256,
                origin=item.origin,
                origin_ref=item.origin_ref,
                object_key=item.object_key,
                created_at=item.created_at,
            )
            session.add(row)
            await session.flush()
            await session.refresh(row)
            entity = _item(row)
            await session.commit()
            return entity

    async def get_item(self, item_id: str) -> Item | None:
        key = _uuid(item_id)
        if key is None:
            return None
        async with self._sessions()() as session:
            row = await session.get(UserDataItem, key)
            return _item(row) if row is not None else None

    async def items(self, ids: list[str]) -> dict[str, Item]:
        keys = [k for k in (_uuid(i) for i in ids) if k is not None]
        if not keys:
            return {}
        async with self._sessions()() as session:
            rows = await session.execute(
                select(UserDataItem).where(UserDataItem.id.in_(keys))
            )
            return {str(r.id): _item(r) for r in rows.scalars()}

    async def list_items(self, owner: Owner) -> list[Item]:
        async with self._sessions()() as session:
            rows = await session.execute(
                select(UserDataItem)
                .where(
                    UserDataItem.owner_subject == owner.subject,
                    UserDataItem.app == owner.app,
                    UserDataItem.deleted_at.is_(None),
                )
                .order_by(UserDataItem.created_at.desc())
            )
            return [_item(r) for r in rows.scalars()]

    async def bytes_used(self, owner: Owner) -> int:
        async with self._sessions()() as session:
            total = await session.scalar(
                select(func.coalesce(func.sum(UserDataItem.size_bytes), 0)).where(
                    UserDataItem.owner_subject == owner.subject,
                    UserDataItem.app == owner.app,
                    UserDataItem.deleted_at.is_(None),
                )
            )
            return int(total or 0)

    async def find_by_origin(self, owner: Owner, origin_ref: str) -> Item | None:
        async with self._sessions()() as session:
            row = await session.scalar(
                select(UserDataItem).where(
                    UserDataItem.owner_subject == owner.subject,
                    UserDataItem.app == owner.app,
                    UserDataItem.origin_ref == origin_ref,
                )
            )
            return _item(row) if row is not None else None

    async def delete_item(self, item_id: str, now: datetime) -> None:
        key = _uuid(item_id)
        if key is None:
            raise not_found()
        async with self._sessions()() as session:
            row = await session.get(UserDataItem, key, with_for_update=True)
            if row is None or row.deleted_at is not None:
                raise not_found()
            granted = await session.scalar(
                select(
                    exists().where(
                        UserDataGrantItem.item_id == key,
                        UserDataGrantItem.grant_id == UserDataGrant.id,
                        UserDataGrant.revoked_at.is_(None),
                        UserDataGrant.expires_at > now,
                    )
                )
            )
            consented = await session.scalar(
                select(
                    exists().where(
                        UserDataConsentItem.item_id == key,
                        UserDataConsentItem.consent_id == UserDataConsent.id,
                        UserDataConsent.withdrawn_at.is_(None),
                    )
                )
            )
            if granted or consented:
                raise conflict(
                    "in_use", "the item is used by a run or a consent and cannot go"
                )
            row.deleted_at = now
            await session.commit()

    async def add_consent(self, consent: Consent) -> Consent:
        async with self._sessions()() as session:
            row = UserDataConsent(
                id=uuid.UUID(consent.id),
                owner_subject=consent.owner.subject,
                app=consent.owner.app,
                purpose=consent.purpose,
                ref=consent.ref,
                created_at=consent.created_at,
            )
            session.add(row)
            await session.flush()
            await self._lock_live(session, consent.pins)
            session.add_all(
                UserDataConsentItem(
                    consent_id=row.id, item_id=uuid.UUID(p.item_id), sha256=p.sha256
                )
                for p in consent.pins
            )
            await session.commit()
            return consent

    async def get_consent(self, consent_id: str) -> Consent | None:
        key = _uuid(consent_id)
        if key is None:
            return None
        async with self._sessions()() as session:
            row = await session.get(UserDataConsent, key)
            if row is None:
                return None
            pins = await session.execute(
                select(UserDataConsentItem).where(UserDataConsentItem.consent_id == key)
            )
            return Consent(
                id=str(row.id),
                owner=Owner(row.owner_subject, row.app),  # type: ignore[arg-type]
                purpose=row.purpose,
                ref=row.ref,
                pins=tuple(
                    Pin(str(p.item_id), p.sha256)
                    for p in sorted(pins.scalars(), key=lambda p: str(p.item_id))
                ),
                created_at=row.created_at,
                withdrawn_at=row.withdrawn_at,
            )

    async def withdraw_consent(self, consent_id: str, now: datetime) -> None:
        key = _uuid(consent_id)
        async with self._sessions()() as session:
            await session.execute(
                update(UserDataConsent)
                .where(
                    UserDataConsent.id == key, UserDataConsent.withdrawn_at.is_(None)
                )
                .values(withdrawn_at=now)
            )
            await session.commit()

    async def add_grant(self, grant: Grant) -> Grant:
        async with self._sessions()() as session:
            await self._lock_live(session, grant.pins)
            row = UserDataGrant(
                id=uuid.UUID(grant.id),
                kind=grant.kind,
                owner_subject=grant.owner.subject,
                app=grant.owner.app,
                reader_app=grant.reader_app,
                ref=grant.ref,
                scope=grant.scope,
                issued_by=grant.issued_by,
                issued_at=grant.issued_at,
                expires_at=grant.expires_at,
                consent_id=uuid.UUID(grant.consent_id) if grant.consent_id else None,
            )
            session.add(row)
            await session.flush()
            session.add_all(
                UserDataGrantItem(
                    grant_id=row.id, item_id=uuid.UUID(p.item_id), sha256=p.sha256
                )
                for p in grant.pins
            )
            await session.commit()
            return grant

    async def get_grant(self, grant_id: str) -> Grant | None:
        key = _uuid(grant_id)
        if key is None:
            return None
        async with self._sessions()() as session:
            row = await session.get(UserDataGrant, key)
            if row is None:
                return None
            pins = await session.execute(
                select(UserDataGrantItem).where(UserDataGrantItem.grant_id == key)
            )
            return Grant(
                id=str(row.id),
                kind=row.kind,  # type: ignore[arg-type]
                owner=Owner(row.owner_subject, row.app),  # type: ignore[arg-type]
                reader_app=row.reader_app,  # type: ignore[arg-type]
                ref=row.ref,
                pins=tuple(
                    Pin(str(p.item_id), p.sha256)
                    for p in sorted(pins.scalars(), key=lambda p: str(p.item_id))
                ),
                scope=row.scope,  # type: ignore[arg-type]
                issued_by=row.issued_by,
                issued_at=row.issued_at,
                expires_at=row.expires_at,
                consent_id=str(row.consent_id) if row.consent_id else None,
                revoked_at=row.revoked_at,
            )

    async def revoke_grant(self, grant_id: str, now: datetime) -> None:
        key = _uuid(grant_id)
        async with self._sessions()() as session:
            await session.execute(
                update(UserDataGrant)
                .where(UserDataGrant.id == key, UserDataGrant.revoked_at.is_(None))
                .values(revoked_at=now)
            )
            await session.commit()

    async def record_read(self, event: ReadEvent) -> None:
        async with self._sessions()() as session:
            session.add(
                UserDataRead(
                    at=event.at,
                    owner_subject=event.owner.subject,
                    app=event.owner.app,
                    item_id=uuid.UUID(event.item_id),
                    sha256=event.sha256,
                    reader=event.reader,
                    operation=event.operation,
                    grant_id=uuid.UUID(event.grant_id) if event.grant_id else None,
                )
            )
            await session.commit()

    @staticmethod
    async def _lock_live(session: AsyncSession, pins: tuple[Pin, ...]) -> None:
        """锁住要引的资料（共享锁，挡住同时进行的删除），并核它们还在、摘要没变。"""
        keys = [uuid.UUID(p.item_id) for p in pins]
        rows = (
            (
                await session.execute(
                    select(UserDataItem)
                    .where(UserDataItem.id.in_(keys))
                    .with_for_update(read=True)
                )
            )
            .scalars()
            .all()
        )
        live = {str(r.id): r for r in rows if r.deleted_at is None}
        for p in pins:
            row = live.get(p.item_id)
            if row is None:
                raise not_found()
            if row.sha256 != p.sha256:
                raise conflict("sha256_mismatch", "the pinned version does not match")
