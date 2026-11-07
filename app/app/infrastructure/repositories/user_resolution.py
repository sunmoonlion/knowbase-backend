"""解析的记录：候选、解析清单、确认、偏好（SDD 0043 第三节）。"""

from __future__ import annotations

import uuid
from collections.abc import Callable

from sqlalchemy import and_, exists, func, or_, select, text
from sqlalchemy.dialects.postgresql import array, insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.domain.chart_of_accounts import CHART
from app.domain.fact_needs import DatedFact, FactKey
from app.domain.user_data import conflict
from app.domain.user_facts import FactRow
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
    UserDataFact,
    UserDataHolding,
    UserDataItem,
    UserDataPreference,
    UserDataRendition,
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

    async def fact_candidates(
        self,
        subject: str,
        app: str,
        key: FactKey,
        *,
        public_access: bool = False,
    ) -> list[tuple[Candidate, list[DatedFact]]]:
        """请的人能用的、这家公司的资料里，表格读法有这一期这几个数的
        （SDD 0051 第 2.2 节）。

        看得见哪些资料和 `candidates` 一样（他名下的、公开原件层）；每份取他能用的
        那一版、那一版最新的就绪表格读法；只返回口径对得上的标准化层的行，带资料的
        披露日。
        """
        shown = exists().where(
            UserDataUse.subject == subject,
            UserDataUse.item_id == UserDataItem.id,
            UserDataUse.use == f"app:{app}",
            UserDataUse.withdrawn_at.is_(None),
        )
        number = func.coalesce(UserDataHolding.version_no, UserDataItem.current_version)
        visible = [
            UserDataItem.subject_code == key.subject,
            UserDataItem.description_status.in_(RESOLVABLE),
            UserDataItem.vocab_version == VOCAB_VERSION,
            UserDataItem.purged_at.is_(None),
        ]
        held = (
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
                or_(
                    UserDataItem.content_origin != "output",
                    UserDataItem.provenance.has_key("converted_from"),
                ),
                *visible,
            )
            .order_by(UserDataItem.created_at, UserDataItem.id)
        )
        async with self._sessions()() as session:
            seen: dict[str, tuple[Candidate, UserDataItem, UserDataVersion]] = {}
            for holding, item, version, is_shown in (await session.execute(held)).all():
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
                if found.material_id not in seen or holding.version_no is None:
                    seen[found.material_id] = (found, item, version)
            if public_access:
                public = select(UserDataItem, UserDataVersion).join(
                    UserDataVersion,
                    and_(
                        UserDataVersion.item_id == UserDataItem.id,
                        UserDataVersion.version_no == UserDataItem.current_version,
                    ),
                )
                public = public.where(
                    UserDataItem.owner_subject == PUBLIC_OWNER, *visible
                )
                for item, version in (await session.execute(public)).all():
                    if str(item.id) in seen:
                        continue
                    seen[str(item.id)] = (
                        Candidate(
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
                        ),
                        item,
                        version,
                    )
            if not seen:
                return []
            versions = [v.id for _, _, v in seen.values()]
            renditions = (
                await session.execute(
                    select(UserDataRendition)
                    .where(
                        UserDataRendition.version_id.in_(versions),
                        UserDataRendition.kind == "table",
                        UserDataRendition.status == "ready",
                    )
                    .order_by(UserDataRendition.rendition_no)
                )
            ).scalars()
            latest = {r.version_id: r for r in renditions}  # 编号大的在后，覆盖前面的
            if not latest:
                return []
            facts = (
                await session.execute(
                    select(UserDataFact).where(
                        UserDataFact.rendition_id.in_([r.id for r in latest.values()]),
                        UserDataFact.code.in_(key.codes),
                        UserDataFact.period_end == key.period_end,
                        UserDataFact.duration == key.duration,
                        UserDataFact.consolidation == key.consolidation,
                    )
                )
            ).scalars()
            by_rendition: dict[uuid.UUID, list[UserDataFact]] = {}
            for fact in facts:
                by_rendition.setdefault(fact.rendition_id, []).append(fact)
            out: list[tuple[Candidate, list[DatedFact]]] = []
            for candidate, item, version in seen.values():
                rendition = latest.get(version.id)
                rows = by_rendition.get(rendition.id, []) if rendition else []
                if rendition is None or not rows:
                    continue
                dated = [
                    DatedFact(
                        FactRow(
                            subject=item.subject_code,
                            code=fact.code,
                            label=CHART[fact.code].name,
                            value=fact.value,
                            unit="元",
                            currency=fact.currency,
                            period_end=fact.period_end,
                            duration=fact.duration,
                            consolidation=fact.consolidation,
                            material_id=str(item.id),
                            material_name=item.name,
                            version_no=fact.version_no,
                            rendition_no=rendition.rendition_no,
                            page=fact.page,
                            layer="standard",
                            ref=f"fact:{fact.id}",
                        ),
                        item.disclosed_on,
                    )
                    for fact in rows
                ]
                out.append(
                    (
                        Candidate(
                            **{
                                **candidate.__dict__,
                                "rendition_kind": "table",
                                "rendition_no": rendition.rendition_no,
                                "rendition_sha256": rendition.sha256,
                            }
                        ),
                        dated,
                    )
                )
            return out

    # —— 解析清单 ——

    async def add_resolution(
        self, resolution: Resolution, *, expected_revision: int
    ) -> Resolution:
        """在既有修订锁内核对基版，再取新编号；不让旧解析覆盖并发提交的需求。"""
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
            if (latest or 0) != expected_revision:
                raise conflict(
                    "resolution_changed",
                    "The data list changed while resolving; reload it before retrying.",
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
    if key.source_query is not None:
        conditions.append(
            UserDataItem.provenance["source_query"].astext == key.source_query
        )
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
    # 定期报告本身披露业绩；采集器按报告期取同一原件，不要求它另挂 results 标签。
    # 仅这一隐含事项可由报告类型满足，其余事项仍按原来的任一命中语义筛选。
    if key.topics and not (is_periodic(key.doc_type) and "results" in key.topics):
        conditions.append(UserDataItem.topics.has_any(array(list(key.topics))))
    if key.industry is not None:
        conditions.append(UserDataItem.industry == key.industry)
    return conditions
