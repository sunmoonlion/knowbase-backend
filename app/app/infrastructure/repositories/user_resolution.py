"""解析的记录：候选、解析清单、确认、偏好（SDD 0043 第三节）。"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from datetime import datetime

from sqlalchemy import and_, exists, func, or_, select, text
from sqlalchemy.dialects.postgresql import array, insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.domain.user_library import PUBLIC_OWNER, RESOLVABLE
from app.domain.user_resolution import (
    Candidate,
    Confirmation,
    DescriptionKey,
    Preference,
    Resolution,
    item_dict,
    item_from,
    need_from,
    params_from,
)
from app.domain.vocabulary import VOCAB_VERSION, is_periodic, types_of
from app.infrastructure.models.user_data import (
    UserDataConfirmation,
    UserDataHolding,
    UserDataItem,
    UserDataPreference,
    UserDataResolution,
    UserDataUse,
    UserDataVersion,
)


def _resolution(row: UserDataResolution) -> Resolution:
    return Resolution(
        id=str(row.id),
        revision=row.revision,
        subject=row.subject,
        app=row.app,
        consumer=row.consumer,
        contract_version=row.contract_version,
        vocab_version=row.vocab_version,
        needs=tuple(need_from(n) for n in row.needs),
        params=params_from(row.params),
        items=tuple(item_from(i) for i in row.items),
        created_at=row.created_at,
        task_ref=row.task_ref,
    )


def _confirmation(row: UserDataConfirmation) -> Confirmation:
    return Confirmation(
        id=str(row.id),
        subject=row.subject,
        app=row.app,
        kind=row.kind,  # type: ignore[arg-type]
        items=tuple(row.items),
        supply=dict(row.supply),
        created_at=row.created_at,
        expires_at=row.expires_at,
        task_ref=row.task_ref,
        resolution_id=str(row.resolution_id) if row.resolution_id else None,
        resolution_revision=row.resolution_revision,
        needs=tuple(row.needs or ()),
    )


class SqlUserResolutionRepository:
    def __init__(
        self, sessions: Callable[[], async_sessionmaker[AsyncSession]]
    ) -> None:
        self._sessions = sessions

    async def candidates(
        self,
        subject: str,
        app: str,
        key: DescriptionKey,
        *,
        public_access: bool = False,
    ) -> list[Candidate]:
        """请的人名下、描述对得上这一条的资料（确认过的描述、当前词表；产出区的不算）。

        每份取他能用的那一版（所有者当前版，买家成交那一版）；禁用的也列出来、标不能用。
        """
        shown = exists().where(
            UserDataUse.subject == subject,
            UserDataUse.item_id == UserDataItem.id,
            UserDataUse.use == f"app:{app}",
            UserDataUse.withdrawn_at.is_(None),
        )
        number = func.coalesce(UserDataHolding.version_no, UserDataItem.current_version)
        query = (
            select(UserDataHolding, UserDataItem, UserDataVersion, shown.label("shown"))
            .join(UserDataItem, UserDataItem.id == UserDataHolding.item_id)
            .join(
                UserDataVersion,
                and_(
                    UserDataVersion.item_id == UserDataItem.id,
                    UserDataVersion.version_no == number,
                ),
            )
            .where(
                UserDataHolding.subject == subject,
                UserDataHolding.ended_at.is_(None),
                UserDataItem.description_status.in_(RESOLVABLE),
                UserDataItem.vocab_version == VOCAB_VERSION,
                UserDataItem.purged_at.is_(None),
                or_(
                    UserDataItem.content_origin != "output",
                    UserDataItem.provenance.has_key("converted_from"),
                ),
                *_key_conditions(key),
            )
            .order_by(UserDataItem.created_at, UserDataItem.id)
        )
        async with self._sessions()() as session:
            rows = await session.execute(query)
            seen: dict[str, Candidate] = {}
            for holding, item, version, is_shown in rows.all():
                # 他名下的公开原件引用：他的应用都能读，不用另外放开
                reference = holding.basis == "reference"
                found = Candidate(
                    material_id=str(item.id),
                    version_no=version.version_no,
                    sha256=version.sha256,
                    content_origin=item.content_origin,
                    disclosed_on=item.disclosed_on,
                    created_at=item.created_at,
                    shown=bool(is_shown) or reference,
                    data_format=item.data_format,
                    usable=item.disabled_at is None,
                    basis="reference" if reference else "display",
                    name=item.name,
                    media_type=version.media_type,
                    size_bytes=version.size_bytes,
                )
                # 同一份有两条使用权（自己的、买的）时取不限版本的那条
                if found.material_id not in seen or holding.version_no is None:
                    seen[found.material_id] = found
            if public_access:
                # 公开原件层全市场可读（0047 第 2.2 节）：
                # 他没有引用的也算候选，凭「公开」
                public = await session.execute(
                    _matching(
                        select(UserDataItem, UserDataVersion)
                        .join(
                            UserDataVersion,
                            and_(
                                UserDataVersion.item_id == UserDataItem.id,
                                UserDataVersion.version_no
                                == UserDataItem.current_version,
                            ),
                        )
                        .where(UserDataItem.owner_subject == PUBLIC_OWNER),
                        key,
                    )
                )
                for item, version in public.all():
                    if str(item.id) in seen:
                        continue
                    seen[str(item.id)] = Candidate(
                        material_id=str(item.id),
                        version_no=version.version_no,
                        sha256=version.sha256,
                        content_origin=item.content_origin,
                        disclosed_on=item.disclosed_on,
                        created_at=item.created_at,
                        shown=True,
                        data_format=item.data_format,
                        usable=item.disabled_at is None,
                        basis="public",
                        name=item.name,
                        media_type=version.media_type,
                        size_bytes=version.size_bytes,
                    )
            return list(seen.values())

    # —— 解析清单 ——

    async def add_resolution(self, resolution: Resolution) -> Resolution:
        """记一个修订：同一张清单排队取下一个修订号。"""
        async with self._sessions()() as session:
            key = uuid.UUID(resolution.id)
            await session.execute(
                text("SELECT pg_advisory_xact_lock(hashtext(:k))"),
                {"k": f"user_data_resolution:{resolution.id}"},
            )
            latest = await session.scalar(
                select(func.max(UserDataResolution.revision)).where(
                    UserDataResolution.id == key
                )
            )
            row = UserDataResolution(
                id=key,
                revision=(latest or 0) + 1,
                subject=resolution.subject,
                app=resolution.app,
                consumer=resolution.consumer,
                contract_version=resolution.contract_version,
                vocab_version=resolution.vocab_version,
                task_ref=resolution.task_ref,
                needs=[n.as_dict() for n in resolution.needs],
                params=resolution.params.as_dict(),
                items=[item_dict(i) for i in resolution.items],
                created_at=resolution.created_at,
            )
            session.add(row)
            await session.flush()
            entity = _resolution(row)
            await session.commit()
            return entity

    async def resolution(
        self, resolution_id: str, revision: int | None = None
    ) -> Resolution | None:
        try:
            key = uuid.UUID(resolution_id)
        except (ValueError, TypeError):
            return None
        async with self._sessions()() as session:
            query = select(UserDataResolution).where(UserDataResolution.id == key)
            if revision is not None:
                query = query.where(UserDataResolution.revision == revision)
            row = await session.scalar(
                query.order_by(UserDataResolution.revision.desc()).limit(1)
            )
            return _resolution(row) if row is not None else None

    # —— 确认 ——

    async def add_confirmation(self, confirmation: Confirmation) -> Confirmation:
        async with self._sessions()() as session:
            session.add(
                UserDataConfirmation(
                    id=uuid.UUID(confirmation.id),
                    subject=confirmation.subject,
                    app=confirmation.app,
                    kind=confirmation.kind,
                    task_ref=confirmation.task_ref,
                    resolution_id=uuid.UUID(confirmation.resolution_id)
                    if confirmation.resolution_id
                    else None,
                    resolution_revision=confirmation.resolution_revision,
                    needs=list(confirmation.needs),
                    items=list(confirmation.items),
                    supply=dict(confirmation.supply),
                    created_at=confirmation.created_at,
                    expires_at=confirmation.expires_at,
                )
            )
            await session.commit()
            return confirmation

    async def confirmation(self, confirmation_id: str) -> Confirmation | None:
        try:
            key = uuid.UUID(confirmation_id)
        except (ValueError, TypeError):
            return None
        async with self._sessions()() as session:
            row = await session.get(UserDataConfirmation, key)
            return _confirmation(row) if row is not None else None

    async def live_confirmations(
        self, subject: str, now: datetime
    ) -> list[Confirmation]:
        async with self._sessions()() as session:
            rows = await session.execute(
                select(UserDataConfirmation)
                .where(
                    UserDataConfirmation.subject == subject,
                    UserDataConfirmation.expires_at > now,
                )
                .order_by(UserDataConfirmation.created_at)
            )
            return [_confirmation(r) for r in rows.scalars()]

    # —— 偏好 ——

    async def preferences(self, subject: str, consumer: str) -> list[Preference]:
        async with self._sessions()() as session:
            rows = await session.execute(
                select(UserDataPreference)
                .where(
                    UserDataPreference.subject == subject,
                    UserDataPreference.consumer == consumer,
                )
                .order_by(UserDataPreference.key_id)
            )
            return [
                Preference(
                    subject=r.subject,
                    consumer=r.consumer,
                    key_id=r.key_id,
                    material_ids=tuple(r.material_ids),
                    vocab_version=r.vocab_version,
                    updated_at=r.updated_at,
                )
                for r in rows.scalars()
            ]

    async def save_preference(self, preference: Preference) -> Preference:
        async with self._sessions()() as session:
            values = {
                "subject": preference.subject,
                "consumer": preference.consumer,
                "key_id": preference.key_id,
                "material_ids": list(preference.material_ids),
                "vocab_version": preference.vocab_version,
                "updated_at": preference.updated_at,
            }
            await session.execute(
                insert(UserDataPreference)
                .values(**values)
                .on_conflict_do_update(
                    index_elements=["subject", "consumer", "key_id"],
                    set_={
                        "material_ids": values["material_ids"],
                        "vocab_version": values["vocab_version"],
                        "updated_at": values["updated_at"],
                    },
                )
            )
            await session.commit()
            return preference


def _matching(query, key: DescriptionKey):
    """描述对得上这一条；确认过的描述、当前词表；没清除。"""
    return query.where(
        UserDataItem.description_status.in_(RESOLVABLE),
        UserDataItem.vocab_version == VOCAB_VERSION,
        UserDataItem.purged_at.is_(None),
        *_key_conditions(key),
    )


def _key_conditions(key: DescriptionKey) -> list:
    """一条描述在资料表上的条件（SDD 0049）：主体（不限主体的不筛）；类型（大类展开成
    它的细类）；版本形态；定期报告按报告期，其余按披露日落在披露时间窗里；事项任一
    对上；行业。"""
    conditions = [
        UserDataItem.doc_type.in_(types_of(key.doc_type)),
        UserDataItem.doc_edition == key.edition,
    ]
    if key.subject is not None:
        conditions.append(UserDataItem.subject_code == key.subject)
    if key.period_end is not None:
        if is_periodic(key.doc_type):
            conditions += [
                UserDataItem.period_start == key.period_start,
                UserDataItem.period_end == key.period_end,
            ]
        else:
            conditions += [
                UserDataItem.disclosed_on >= key.period_start,
                UserDataItem.disclosed_on <= key.period_end,
            ]
    if key.topics:
        conditions.append(UserDataItem.topics.has_any(array(list(key.topics))))
    if key.industry is not None:
        conditions.append(UserDataItem.industry == key.industry)
    return conditions

    async def record_preference(
        self,
        subject: str,
        consumer: str,
        key_id: str,
        material_ids: list[str],
        vocab_version: int,
        updated_at: datetime,
    ) -> Preference:
        async with self._sessions()() as session:
            stmt = (
                insert(UserDataPreference)
                .values(
                    subject=subject,
                    consumer=consumer,
                    key_id=key_id,
                    material_ids=material_ids,
                    vocab_version=vocab_version,
                    updated_at=updated_at,
                )
                .on_conflict_do_update(
                    index_elements=["subject", "consumer", "key_id"],
                    set_={
                        "material_ids": material_ids,
                        "vocab_version": vocab_version,
                        "updated_at": updated_at,
                    },
                )
            )
            await session.execute(stmt)
            await session.commit()
            return Preference(
                subject=subject,
                consumer=consumer,
                key_id=key_id,
                material_ids=tuple(material_ids),
                vocab_version=vocab_version,
                updated_at=updated_at,
            )
