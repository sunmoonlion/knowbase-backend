"""运行：运行授权（第四版）、凭授权读、加工写回读法、产出区、禁用与清除。

SDD 0043 块 1c。

谁能做什么：
- 用户本人（应用凭本人令牌转交）：签委托、会话、孵化的授权；撤自己的授权；
  把产出转为数据；看自己资料的读数留痕；撤回平台过程的依据（授权一并撤）。
- knowlab（服务身份）：凭展示依据签测试授权，凭 `platform:` 依据签平台过程授权
  （运营、机器分开签）；过程结束、不受理时撤。
- knowbench、knowlab（服务身份）：替用户往产出区写，按（任务，运行，名字）幂等。
- knowfeed（服务身份）：凭本人的加工依据领加工授权，读那一版、写回读法。正文、按页文字
  存进 PostgreSQL（0043 第五节，数据拓扑第 16.2 节），表格读法是一批事实行。
- 执行的应用：凭授权读钉住的那一版（或那个读法），用完交还。
- 管理端：禁用（违规处置）、注销与依法删除。
"""

from __future__ import annotations

import hashlib
import re
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime

from app.application.ports.user_data import UserObjects
from app.application.ports.user_library import UserLibraryRepository
from app.application.ports.user_runs import UserRunRepository
from app.domain.chart_of_accounts import CHART_VERSION, METRICS
from app.domain.public_access import PublicGate
from app.domain.user_data import (
    FormatView,
    UserDataError,
    check_size,
    clean_name,
    conflict,
    format_view,
    kind_of,
    not_found,
    refused,
)
from app.domain.user_facts import (
    MAX_RESULT_ROWS,
    FactQuery,
    FactRow,
    MetricRow,
    RawFact,
    StandardFact,
    batch_digest,
    check_batch,
    compute_metric,
    quality,
)
from app.domain.user_library import (
    PLATFORM_KINDS,
    PUBLIC_OWNER,
    ContentVersion,
    Description,
    Holding,
    License,
    Material,
    Rendition,
    check_license,
    frozen_expert_use,
    license_rank,
    platform_use,
    processing_use,
    version_object_key,
)
from app.domain.user_readings import (
    Page,
    TextReading,
    pages_bytes,
    pages_digest,
    parse_pages,
    parse_text,
    text_digest,
)
from app.domain.user_resolution import Resolution
from app.domain.user_runs import (
    BASES_OF,
    DISPLAY_APP_OF,
    SCOPE_OF,
    SESSION_HINT_AT,
    SESSION_TTL,
    USER_PROCESSES,
    GrantItem,
    GrantRequestItem,
    InputPin,
    InputSet,
    Output,
    RunGrant,
    Session,
    check_pins,
    check_ref,
    denied,
    grant_expiry,
    input_set_id,
)
from app.domain.vocabulary import VOCAB_VERSION, format_of_media

OUTPUT_CALLERS = ("knowbench", "knowlab")
# 专家与测试的运行方（knowbot-test-run 契约）：模型经它读数据
RUNTIME_APP = "knowbench"
PROCESSORS = ("knowfeed",)
# knowfeed 写回的读法；扫描版与检索索引由 knowbase 经 RAGFlow 做（块 9）。
# 表格的内容走事实批（write_facts），这里只收它没成的
PROCESSOR_KINDS = ("text", "pages", "table")
# 找证据时一次最多取多少页来打分（块 9 换成 RAGFlow 的混合检索）
MAX_SEARCH_PAGES = 1000
_FROZEN = re.compile(r"^expert:[A-Za-z0-9._-]{1,64}@[0-9][0-9.]{0,31}$")
_PROVENANCE_KEYS = ("expert_id", "expert_version", "input_set_id")
# 块 9（RAGFlow 删除同步）之前，真删要等的回执
PURGE_PENDING = ["ragflow", "knowfeed"]


@dataclass(frozen=True)
class ReadResult:
    item: GrantItem
    version: ContentVersion
    media_type: str
    content: bytes | None
    format: FormatView | None = None
    rendition: Rendition | None = None


@dataclass(frozen=True)
class Reading:
    """读到的一种读法：正文给 `text`；按页文字给要的那几页与总页数。"""

    item: GrantItem
    version: ContentVersion
    rendition: Rendition
    text: TextReading | None = None
    pages: tuple[Page, ...] = ()
    total_pages: int = 0


class UserRunService:
    def __init__(
        self,
        library: UserLibraryRepository,
        runs: UserRunRepository,
        objects: UserObjects,
        *,
        prefix: str,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        new_id: Callable[[], str] = lambda: str(uuid.uuid4()),
        gate: PublicGate | None = None,
    ) -> None:
        self.library = library
        self.runs = runs
        self.objects = objects
        self.prefix = prefix
        self.now = clock
        self.new_id = new_id
        # 公开原件全市场读、跨全市场查事实的一道门：资格、权限、平台总开关（0047）
        self.gate = gate or PublicGate()

    # —— 签发 ——

    async def grant_by_user(
        self,
        subject: str,
        app: str,
        process: str,
        ref: str,
        items: list[GrantRequestItem],
        ttl_seconds: int | None = None,
    ) -> RunGrant:
        """本人在应用页面上签：委托、会话（knowbench），孵化（knowlab）。

        同一件事（ref）再签就是运行中追加：新签一条独立授权，已签的不扩大。
        """
        if process not in USER_PROCESSES:
            raise refused("process_invalid", "this process is not granted by users")
        if app != DISPLAY_APP_OF[process]:
            raise refused(
                "app_invalid", f"a {process} runs in {DISPLAY_APP_OF[process]}"
            )
        return await self._issue(
            process,
            executor_app=app,
            beneficiary=subject,
            issued_by=subject,
            ref=ref,
            items=items,
            reader_role="app",
            ttl_seconds=ttl_seconds,
            appendable=True,
        )

    async def grant_by_confirmation(
        self,
        caller: str,
        subject: str,
        confirmation_id: str,
        ref: str,
        resolution: Resolution,
        items: list[GrantRequestItem],
        ttl_seconds: int | None = None,
        confirmed_needs: frozenset[str] = frozenset(),
    ) -> RunGrant:
        """等数据的委托数据齐了：执行的应用凭本人那次确认续签（所有者 10-27 定「自动
        接着跑」，0043 第 3.4 节）。确认与清单由调用方先核过（`confirmed_for_run`）。

        签进去的只能是这一修订清单里、确认时就有的需求项的候选（依据也照清单），或
        作者交付时冻结给这位专家这一版的绑定；逐项再按本人签时同一套规矩核。
        """
        if caller != DISPLAY_APP_OF["delegation"]:
            raise refused("app_invalid", "a delegation runs in knowbench")
        offered = {
            (c.material_id, c.version_no, c.sha256): c
            for item in resolution.items
            if item.need_key in confirmed_needs
            for c in item.chosen + item.candidates
        }
        for request in items:
            pin = request.pin
            if request.basis == "frozen_binding":
                if request.basis_ref != resolution.consumer:
                    raise denied(
                        "binding_mismatch", "a binding is for another expert version"
                    )
                continue
            offer = offered.get((pin.material_id, pin.version_no, pin.sha256))
            if offer is None or offer.basis != request.basis:
                raise denied("outside_resolution", "an item is not on the list")
            if pin.rendition_kind is not None and (
                offer.rendition_kind,
                offer.rendition_no,
                offer.rendition_sha256,
            ) != (pin.rendition_kind, pin.rendition_no, pin.rendition_sha256):
                raise denied("outside_resolution", "a reading is not on the list")
        return await self._issue(
            "delegation",
            executor_app=caller,
            beneficiary=subject,
            issued_by=f"confirmation:{confirmation_id}",
            ref=ref,
            items=items,
            reader_role="app",
            ttl_seconds=ttl_seconds,
            appendable=True,
        )

    # —— 会话：本人登记一次，模型首读一份时签小授权（0048 第五节） ——

    async def register_session(self, subject: str, app: str) -> Session:
        """本人经应用登记一段会话（24 小时）；范围是他放开给这个应用的。"""
        if app != DISPLAY_APP_OF["session"]:
            raise refused("app_invalid", "sessions run in knowbench")
        now = self.now()
        return await self.runs.add_session(
            Session(
                id=self.new_id(),
                subject=subject,
                executor_app=app,
                scope_use=f"app:{app}",
                created_at=now,
                expires_at=now + SESSION_TTL,
            )
        )

    async def end_session(self, subject: str, app: str, session_id: str) -> int:
        """会话结束：小授权一并撤。"""
        session = await self._own_session(subject, app, session_id)
        now = self.now()
        await self.runs.end_session(session.id, now)
        ids = await self.runs.grant_ids(executor_app=app, ref=session.id)
        return await self.runs.revoke(ids, now, "session-ended")

    async def session_read(
        self, subject: str, app: str, session_id: str, material_id: str
    ) -> tuple[RunGrant, int, bool]:
        """模型在会话里读一份：读过的回原来那条；第一次读时核在范围里，当场签一条
        小授权（钉住他能用的那一版）。返回授权、这段会话累计读了几份、要不要提示
        （累计刚过 50 份时提示一次，不挡）。"""
        session = await self._own_session(subject, app, session_id)
        now = self.now()
        existing = await self.runs.grant_ids(
            executor_app=app, ref=session.id, material_id=material_id, active_at=now
        )
        if existing:
            grant = await self.runs.get_grant(existing[0])
            assert grant is not None
            total = len(await self.runs.grant_ids(executor_app=app, ref=session.id))
            return grant, total, False
        material = await self.library.get_material(material_id)
        if material is None or not material.readable:
            raise not_found()
        holdings = await self.library.holdings_of(subject, material.id)
        if material.owner == PUBLIC_OWNER:
            # 公开原件：他名下有引用就凭引用，否则过了门凭「公开」读当前版
            reference = next((h for h in holdings if h.basis == "reference"), None)
            if reference is None:
                self.gate.check(subject, material)
            number = (reference.version_no if reference else None) or (
                material.current_version
            )
            basis, basis_ref = (
                ("reference", "reference") if reference else ("public", PUBLIC_OWNER)
            )
        else:
            if not holdings:
                raise not_found()
            holding = next((h for h in holdings if h.version_no is None), holdings[0])
            number = holding.version_no or material.current_version
            if not await self.runs.has_use(subject, material.id, session.scope_use):
                raise denied("not_shown", f"not shown to {app}")
            basis, basis_ref = "display", session.scope_use
        version = await self.library.version(material.id, number)
        if version is None:
            raise not_found("version")
        remaining = int((session.expires_at - now).total_seconds())
        grant = await self._issue(
            "session",
            executor_app=app,
            beneficiary=subject,
            issued_by=f"session:{session.id}",
            ref=session.id,
            items=[
                GrantRequestItem(
                    pin=InputPin(material.id, version.version_no, version.sha256),
                    basis=basis,  # type: ignore[arg-type]
                    basis_ref=basis_ref,
                )
            ],
            reader_role="app",
            ttl_seconds=max(remaining, 1),
            appendable=True,
        )
        total = len(await self.runs.grant_ids(executor_app=app, ref=session.id))
        return grant, total, total - 1 < SESSION_HINT_AT <= total

    async def grant_by_service(
        self,
        caller: str,
        process: str,
        beneficiary: str,
        ref: str,
        items: list[GrantRequestItem],
        *,
        reader_role: str = "app",
        ttl_seconds: int | None = None,
        platform: str | None = None,
    ) -> RunGrant:
        """knowlab 签：测试凭展示依据（固定测试集，不许中途追加）；平台过程凭
        `platform:` 依据，读的一方分运营与机器，上限按过程配。机器重跑的一条用例可以
        没有凭平台同意的项（全是公开原件、引用、冻结绑定），过程由 platform 给。

        执行的应用是真去读的那一方（0048 第六节）：测试与机器由运行方 knowbench 跑，
        运营在 knowlab 管理端看；签发方都记 knowlab。"""
        if caller != "knowlab":
            raise denied("service_scope_denied", "only knowlab signs these grants")
        if process == "test":
            if reader_role != "app":
                raise refused("reader_role_invalid", "a test is read by the app")
            return await self._issue(
                "test",
                executor_app=RUNTIME_APP,
                beneficiary=beneficiary,
                issued_by=caller,
                ref=ref,
                items=items,
                reader_role="app",
                ttl_seconds=ttl_seconds,
                appendable=False,
            )
        if process != "platform":
            raise refused("process_invalid", "this process is not granted by services")
        if reader_role not in ("operator", "machine"):
            raise refused(
                "reader_role_invalid", "a platform grant is for an operator or machine"
            )
        if reader_role == "operator" and any(i.basis != "platform" for i in items):
            raise refused(
                "basis_not_allowed", "operators read platform-consented items only"
            )
        uses = {i.basis_ref for i in items if i.basis == "platform"}
        if platform is not None:
            uses.add(platform)
        if len(uses) != 1:
            raise refused("basis_ref_invalid", "one platform process per grant")
        use = uses.pop()
        if not any(i.basis == "platform" for i in items) and not [
            u for u in await self.library.named(beneficiary, use) if u.use == use
        ]:
            # 只带公开原件、引用、冻结绑定的：这次过程本人的同意也要还在
            raise denied("outside_consent", "the platform process is not consented")
        kind = _platform_kind(use)
        return await self._issue(
            "platform",
            executor_app="knowlab" if reader_role == "operator" else RUNTIME_APP,
            beneficiary=beneficiary,
            issued_by=caller,
            ref=ref,
            items=items,
            reader_role=reader_role,
            ttl_seconds=ttl_seconds,
            appendable=True,
            platform_use=use,
            platform_kind=kind,
        )

    # —— 撤、交还 ——

    async def revoke_by_user(self, subject: str, grant_id: str) -> None:
        grant = await self.runs.get_grant(grant_id)
        if grant is None or grant.beneficiary != subject:
            raise not_found("grant")
        await self.runs.revoke([grant.id], self.now(), "user")

    async def revoke_by_service(self, caller: str, grant_id: str) -> None:
        grant = await self.runs.get_grant(grant_id)
        if grant is None or grant.issued_by != caller:
            raise not_found("grant")
        await self.runs.revoke([grant.id], self.now(), "issuer")

    async def release(self, executor_app: str, grant_id: str) -> None:
        """执行的应用交还给自己的授权（只会收窄，不用本人在场）。"""
        grant = await self.runs.get_grant(grant_id)
        if grant is None or grant.executor_app != executor_app:
            raise not_found("grant")
        await self.runs.revoke([grant.id], self.now(), "released")

    async def release_ref(self, executor_app: str, ref: str) -> int:
        """会话、任务结束：这件事签过的全部交还。"""
        check_ref(ref)
        ids = await self.runs.grant_ids(executor_app=executor_app, ref=ref)
        return await self.runs.revoke(ids, self.now(), "released")

    async def withdraw_platform(
        self, subject: str, app: str, kind: str, ref: str
    ) -> int:
        """本人撤回平台过程的依据：依据与凭它签的全部授权立即撤。"""
        use = platform_use(app, kind, ref)
        withdrawn = await self.library.withdraw_named(subject, use, self.now())
        await self._revoke_platform(subject, use, "withdrawn")
        return withdrawn

    async def end_platform(
        self, caller: str, subject: str, kind: str, ref: str, reason: str
    ) -> int:
        """knowlab 报过程结束、不受理：依据与授权立即撤。"""
        if caller != "knowlab":
            raise denied("service_scope_denied", "only knowlab ends platform processes")
        if reason not in ("finished", "rejected", "cancelled"):
            raise refused("reason_invalid", "reason is invalid")
        use = platform_use(caller, kind, ref)
        withdrawn = await self.library.withdraw_named(subject, use, self.now())
        await self._revoke_platform(subject, use, reason)
        return withdrawn

    async def void_frozen(
        self, caller: str, author: str, expert_id: str, version: str
    ) -> int:
        """作废没交成的那次冻结（决定 10-30）：作者撤回、平台作废「没交到 knowmart」的
        交付时，knowlab 来作废，下次提交照那时的绑定重新冻结。有作者以外的人凭它签过
        授权（请过这一版）的不许作废。回撤了几份。"""
        if caller != "knowlab":
            raise denied("service_scope_denied", "only knowlab voids frozen bindings")
        frozen = frozen_expert_use(expert_id, version)
        if await self.runs.frozen_used_by_others(author, frozen):
            raise conflict("frozen_in_use", "others have used this frozen binding")
        return await self.library.withdraw_named(author, frozen, self.now())

    async def platform_process(
        self, caller: str, subject: str, kind: str, ref: str
    ) -> int:
        """本人给这次平台过程留的依据还有几份有效（只读）。knowlab 后台核在审的交付：
        作者在 knowbase 总栏撤了审核授权（0 份），knowlab 照作者撤回处理（knowbase 按
        架构不调别的应用，由 knowlab 来查）。"""
        if caller != "knowlab":
            raise denied(
                "service_scope_denied", "only knowlab reads platform processes"
            )
        use = platform_use(caller, kind, ref)
        return len([u for u in await self.library.named(subject, use) if u.use == use])

    # —— 凭授权读 ——

    async def read(
        self,
        executor_app: str,
        reader: str,
        grant_id: str,
        material_id: str,
        version_no: int | None,
        what: str,
    ) -> ReadResult:
        """读钉住的那一版：`content` 原件、`format` 只看格式、`rendition` 钉住的读法。

        授权在、给这个应用、没撤没过期、钉了这一份；资料没禁用没清除；摘要对得上。
        所有者删了的照旧能读（买家、钉住的测试与专家版本照旧可读，0043 第 2.8 节）。
        """
        if what not in ("content", "format", "rendition"):
            raise refused("what_invalid", "read content, format or rendition")
        grant = await self.runs.get_grant(grant_id)
        if grant is None or grant.executor_app != executor_app:
            raise not_found("grant")
        if not grant.active(self.now()):
            raise denied("grant_inactive", "the grant is revoked or expired")
        item = grant.item(material_id, version_no)
        if item is None:
            raise not_found()
        if what != "format" and grant.scope != "content":
            raise denied("format_only", "this grant reads formats only")
        material = await self.library.get_material(item.material_id)
        if material is None or not material.readable:
            raise not_found()
        pins = await self._pins(grant, item)
        version = await self.library.version(item.material_id, item.version_no)
        if version is None or version.sha256 != pins[0].sha256:
            raise conflict("sha256_mismatch", "the pinned version does not match")
        operation = "process" if grant.process == "processing" else what
        if what == "rendition":
            pin = next((p for p in pins if p.rendition_kind), None)
            if pin is None:
                raise not_found("rendition")
            rendition = await self.runs.rendition(
                version.id, pin.rendition_kind or "", pin.rendition_no or 0
            )
            if rendition is None or rendition.sha256 != pin.rendition_sha256:
                raise conflict("sha256_mismatch", "the pinned rendition does not match")
            # 正文、按页文字在库里（0043 第五节）；表格读法是事实行，按运行查事实
            if rendition.kind == "text":
                text = await self.runs.reading_text(rendition.id)
                if text is None:
                    raise not_found("rendition")
                media_type, content = "text/plain; charset=utf-8", text.body.encode()
            elif rendition.kind == "pages":
                pages = await self.runs.reading_pages(rendition.id)
                media_type, content = "application/json", pages_bytes(pages)
            else:
                raise refused("kind_invalid", "read a table reading as facts")
            await self._log(grant, item, version, reader, operation)
            return ReadResult(item, version, media_type, content, rendition=rendition)
        content = await self._fetch(version.object_key, version.sha256)
        await self._log(grant, item, version, reader, operation)
        if what == "format":
            return ReadResult(
                item,
                version,
                version.media_type,
                None,
                format=format_view(version.media_type, content),
            )
        return ReadResult(item, version, version.media_type, content)

    async def read_reading(
        self,
        executor_app: str,
        reader: str,
        grant_id: str,
        material_id: str,
        version_no: int | None,
        kind: str,
        *,
        page_from: int | None = None,
        page_to: int | None = None,
    ) -> Reading:
        """读钉住那一版的某种读法（正文 `text`、按页文字 `pages`，可只取几页）。

        钉了这种读法的读那一版，没钉的读最新就绪的一版（出处里写明读法版本）；
        授权、留痕同 `read`。
        """
        if kind not in ("text", "pages"):
            raise refused("kind_invalid", "read text or pages")
        grant = await self.runs.get_grant(grant_id)
        if grant is None or grant.executor_app != executor_app:
            raise not_found("grant")
        if not grant.active(self.now()):
            raise denied("grant_inactive", "the grant is revoked or expired")
        item = grant.item(material_id, version_no)
        if item is None:
            raise not_found()
        version, rendition = await self._reading_of(grant, item, kind)
        if kind == "text":
            text = await self.runs.reading_text(rendition.id)
            if text is None:
                raise not_found("rendition")
            await self._log(grant, item, version, reader, "rendition")
            return Reading(item, version, rendition, text=text)
        pages = await self.runs.reading_pages(rendition.id, page_from, page_to)
        total = await self.runs.page_count(rendition.id)
        await self._log(grant, item, version, reader, "rendition")
        return Reading(item, version, rendition, pages=tuple(pages), total_pages=total)

    async def search_pages(
        self,
        executor_app: str,
        reader: str,
        targets: list[tuple[RunGrant, GrantItem]],
        terms: list[str],
    ) -> tuple[list[tuple[Reading, Page]], int]:
        """在这几项钉住的按页文字里找含任一关键词的页（数据 MCP 的 `search_evidence`）。

        授权与读法的选法同 `read_reading`；还没有按页文字的跳过。命中的每份记一条读数
        （同查事实：只记真交出去的那几份）。返回命中的页、找了几份。
        """
        now = self.now()
        found: dict[str, tuple[RunGrant, Reading]] = {}
        for grant, item in targets:
            if grant.executor_app != executor_app or not grant.active(now):
                continue
            try:
                version, rendition = await self._reading_of(grant, item, "pages")
            except UserDataError:
                continue
            found[rendition.id] = (grant, Reading(item, version, rendition))
        if not found or not terms:
            return [], len(found)
        hits: list[tuple[Reading, Page]] = []
        logged: set[str] = set()
        for rendition_id, page in await self.runs.search_pages(
            list(found), terms, MAX_SEARCH_PAGES
        ):
            grant, reading = found[rendition_id]
            if rendition_id not in logged:
                logged.add(rendition_id)
                await self._log(
                    grant, reading.item, reading.version, reader, "rendition"
                )
            hits.append((reading, page))
        return hits, len(found)

    async def run_grants(
        self, executor_app: str, beneficiary: str, run_ref: str
    ) -> list[RunGrant]:
        """这件事下他还有效的授权（数据 MCP 列输入、找是哪条授权用）。"""
        return await self._run_grants(executor_app, beneficiary, run_ref)

    async def session_grants(
        self, subject: str, app: str, session_id: str
    ) -> list[RunGrant]:
        """这段会话里已经签过的小授权（会话里找证据只在读过的里找）。"""
        session = await self._own_session(subject, app, session_id)
        out = []
        for grant_id in await self.runs.grant_ids(
            executor_app=app, ref=session.id, active_at=self.now()
        ):
            grant = await self.runs.get_grant(grant_id)
            if grant is not None:
                out.append(grant)
        return out

    async def renditions_of(self, material_id: str, version_no: int) -> list[Rendition]:
        """这一版有哪些读法（列输入时告诉模型能读什么）；调用方已核过授权。"""
        version = await self.library.version(material_id, version_no)
        return [] if version is None else await self.runs.renditions(version.id)

    async def grant_view(
        self, executor_app: str, grant_id: str
    ) -> tuple[RunGrant, InputSet]:
        """执行的应用看这条授权钉了哪几份、凭什么（列清单、写清单 v2 用）。"""
        grant = await self.runs.get_grant(grant_id)
        if grant is None or grant.executor_app != executor_app:
            raise not_found("grant")
        input_set = await self.runs.input_set(grant.input_set_id)
        if input_set is None:
            raise not_found("input_set")
        return grant, input_set

    async def reads(self, subject: str, material_id: str) -> list[dict[str, object]]:
        """谁读过：这份资料的读数留痕；只给所有者看。"""
        material = await self.library.get_material(material_id)
        if material is None or material.owner != subject or material.purged_at:
            raise not_found()
        return await self.runs.reads_of(material.id)

    async def impact(self, subject: str, material_id: str) -> dict[str, object]:
        """删、改低许可之前列出受影响的（0044 第 2.2 节、0043 第 2.8 节）。

        买家与引用的使用权、冻结在专家版本里的绑定、钉住它的在跑授权都照旧可读，
        但要让本人先看到；交给 knowmart 的、绑给专家（下一版）的会跟着失效。
        """
        material = await self.library.get_material(material_id)
        if material is None or material.owner != subject or not material.live:
            raise not_found()
        uses = [u.use for u in await self.library.uses_of(subject, material.id)]
        grants = await self.runs.grant_ids(
            material_id=material.id, active_at=self.now()
        )
        return {
            "other_holders": await self.runs.other_holders(material.id, subject),
            "market": "market" in uses,
            "bound_experts": sorted(
                u.removeprefix("expert:")
                for u in uses
                if u.startswith("expert:") and "@" not in u
            ),
            "frozen_experts": sorted(
                u.removeprefix("expert:")
                for u in uses
                if u.startswith("expert:") and "@" in u
            ),
            "active_grants": len(grants),
        }

    async def platform_overview(self, subject: str) -> list[dict[str, object]]:
        """我给平台的授权（0044 第 2.6 节）：每次过程一条，哪几份、到什么时候、运营
        读过几次。只列还有效的；撤回、过程结束的不再列。"""
        grouped: dict[str, list] = {}
        for use in await self.library.named(subject, "platform:"):
            grouped.setdefault(use.use, []).append(use)
        found: list[dict[str, object]] = []
        for name, uses in grouped.items():
            _, app, kind, ref = name.split(":", 3)
            until, reads = await self.runs.platform_summary(subject, name)
            found.append(
                {
                    "app": app,
                    "kind": kind,
                    "ref": ref,
                    "created_at": min(u.created_at for u in uses),
                    "until": until,
                    "operator_reads": reads,
                    "items": [
                        {"material_id": u.material_id, "version_no": u.version_no}
                        for u in uses
                    ],
                }
            )
        return found

    # —— 加工：knowfeed 领授权、读、写回读法 ——

    async def pending_processing(
        self, caller: str, limit: int = 50
    ) -> list[tuple[str, str, int]]:
        """有加工依据、还没有 knowfeed 读法的那几版（所有者、资料、版本）。"""
        self._processor(caller)
        pending = []
        for owner, material_id, version_no in await self.runs.live_processing_uses(
            max(1, min(limit, 200)) * 2
        ):
            version = await self.library.version(material_id, version_no)
            if version is None:
                continue
            done = [
                r
                for r in await self.runs.renditions(version.id)
                if r.processor in PROCESSORS
            ]
            if not done:
                pending.append((owner, material_id, version_no))
            if len(pending) >= limit:
                break
        return pending

    async def issue_processing(
        self, caller: str, owner: str, material_id: str, version_no: int
    ) -> RunGrant:
        """knowfeed 凭本人的加工依据领一条：只这一版、只这个目的、3 天。"""
        self._processor(caller)
        version = await self.library.version(material_id, version_no)
        if version is None:
            raise not_found("version")
        return await self._issue(
            "processing",
            executor_app=caller,
            beneficiary=owner,
            issued_by="knowbase",
            ref=processing_use(version.id),
            items=[
                GrantRequestItem(
                    pin=InputPin(material_id, version_no, version.sha256),
                    basis="processing",
                    basis_ref=processing_use(version.id),
                )
            ],
            reader_role="system",
            ttl_seconds=None,
            appendable=True,
        )

    async def write_rendition(
        self,
        caller: str,
        grant_id: str,
        kind: str,
        processor_version: str,
        content: bytes | None,
        detail: dict[str, object] | None = None,
        *,
        title: str | None = None,
        published_on: date | None = None,
    ) -> Rendition:
        """knowfeed 写回读法：只能凭加工授权、只能写那一版。

        正文（可带标题、发布日期）存一行，按页文字每页一行，都在 PostgreSQL（0043
        第五节）；读法版本的摘要按规范化内容算。表格的内容走 `write_facts`，这里只收
        它没成的。没成的不带内容、写明原因。
        """
        self._processor(caller)
        if kind not in PROCESSOR_KINDS:
            raise refused("kind_invalid", "knowfeed writes text, pages or table")
        if kind != "text" and (title is not None or published_on is not None):
            raise refused("title_not_allowed", "only a text reading has a title")
        if not processor_version or len(processor_version) > 40:
            raise refused("processor_version_invalid", "processor_version is invalid")
        grant = await self.runs.get_grant(grant_id)
        if (
            grant is None
            or grant.executor_app != caller
            or grant.process != "processing"
        ):
            raise not_found("grant")
        if not grant.active(self.now()):
            raise denied("grant_inactive", "the grant is revoked or expired")
        item = grant.items[0]
        version = await self.library.version(item.material_id, item.version_no)
        material = await self.library.get_material(item.material_id)
        if version is None or material is None or not material.readable:
            raise not_found()
        now = self.now()
        info = dict(detail or {})
        sha256 = text = None
        pages: tuple[Page, ...] = ()
        if content is not None:
            if not content:
                raise refused("empty", "rendition is empty")
            if kind == "text":
                text, removed = parse_text(content, title, published_on)
                sha256 = text_digest(text)
            elif kind == "pages":
                pages, removed = parse_pages(content)
                sha256 = pages_digest(pages)
                info["pages"] = len(pages)
            else:
                raise refused("kind_invalid", "write a table reading as facts")
            if removed:
                info["nul_removed"] = removed
        elif not info.get("reason"):
            raise refused("reason_required", "a failed rendition says why")
        return await self.runs.add_rendition(
            Rendition(
                id=self.new_id(),
                version_id=version.id,
                kind=kind,  # type: ignore[arg-type]
                rendition_no=0,
                processor=caller,
                processor_version=processor_version,
                status="ready" if content is not None else "failed",
                created_at=now,
                sha256=sha256,
                detail=info,
                ready_at=now if content is not None else None,
            ),
            text=text,
            pages=pages,
        )

    async def write_facts(
        self,
        caller: str,
        grant_id: str,
        processor_version: str,
        raw: list[RawFact],
        standard: list[StandardFact],
    ) -> Rendition:
        """knowfeed 写回抽好的表：凭加工授权，原样层与标准化层一批写进一个表格读法版本；
        恒等式质检照实记在读法上，不改数（0047 第 4.3 节）。"""
        self._processor(caller)
        if not processor_version or len(processor_version) > 40:
            raise refused("processor_version_invalid", "processor_version is invalid")
        grant = await self.runs.get_grant(grant_id)
        if (
            grant is None
            or grant.executor_app != caller
            or grant.process != "processing"
        ):
            raise not_found("grant")
        if not grant.active(self.now()):
            raise denied("grant_inactive", "the grant is revoked or expired")
        item = grant.items[0]
        version = await self.library.version(item.material_id, item.version_no)
        material = await self.library.get_material(item.material_id)
        if version is None or material is None or not material.readable:
            raise not_found()
        raw, standard = check_batch(raw, standard)
        now = self.now()
        checks = quality(standard)
        return await self.runs.add_fact_batch(
            Rendition(
                id=self.new_id(),
                version_id=version.id,
                kind="table",
                rendition_no=0,
                processor=caller,
                processor_version=processor_version,
                status="ready",
                created_at=now,
                sha256=batch_digest(raw, standard),
                detail={
                    "chart_version": CHART_VERSION,
                    "raw_rows": len(raw),
                    "standard_rows": len(standard),
                    "quality": checks,
                    "quality_passed": all(c["passed"] for c in checks),
                },
                ready_at=now,
            ),
            material.id,
            version.version_no,
            material.description.subject,
            raw,
            standard,
        )

    # —— 查事实（0047 第 4.4 节） ——

    async def query_facts(
        self,
        executor_app: str,
        reader: str,
        beneficiary: str,
        run_ref: str,
        query: FactQuery,
    ) -> list[FactRow]:
        """查这次运行钉住的那几份的事实：钉了表格读法的查那一版，没钉的查那个内容版本
        最新就绪的表格读法（出处里写明用的哪一版读法）；每份留一条读数。"""
        query = query.checked()
        renditions: dict[str, tuple[RunGrant, GrantItem, ContentVersion]] = {}
        for grant in await self._run_grants(executor_app, beneficiary, run_ref):
            if grant.scope != "content":
                continue
            input_set = await self.runs.input_set(grant.input_set_id)
            for item in grant.items:
                material = await self.library.get_material(item.material_id)
                version = await self.library.version(item.material_id, item.version_no)
                if material is None or not material.readable or version is None:
                    continue
                pinned = next(
                    (
                        p
                        for p in (input_set.pins if input_set else ())
                        if p.material_id == item.material_id
                        and p.version_no == item.version_no
                        and p.rendition_kind == "table"
                    ),
                    None,
                )
                table = (
                    await self.runs.rendition(
                        version.id, "table", pinned.rendition_no or 0
                    )
                    if pinned
                    else await self.runs.latest_table(version.id)
                )
                if table is not None and table.status == "ready":
                    renditions[table.id] = (grant, item, version)
        if not renditions:
            return []
        rows = await self.runs.facts(query, rendition_ids=list(renditions))
        for grant, item, version in {
            (g.id, i.material_id): (g, i, v) for g, i, v in renditions.values()
        }.values():
            if any(r.material_id == item.material_id for r in rows):
                await self._log(grant, item, version, reader, "rendition")
        return rows

    async def query_market(self, subject: str, query: FactQuery) -> list[FactRow]:
        """跨全市场查公开原件的事实（筛选、排序、排名、统计）：过资格、权限、平台总开关
        那道门；条件由用户或他的专家给（决定 ⑪）。只查公开原件的行（权限）。
        公开数据不逐份记读数。"""
        self.gate.check(subject)
        return await self.runs.facts(query.checked())

    async def query_metric(
        self,
        metric_key: str,
        query: FactQuery,
        *,
        subject: str | None = None,
        executor_app: str | None = None,
        reader: str | None = None,
        beneficiary: str | None = None,
        run_ref: str | None = None,
    ) -> list[MetricRow]:
        """按指标算：给 `run_ref` 的只用钉住的那几份，不给的查全市场公开原件（`subject`
        是谁在查，过那道门）。"""
        metric = METRICS.get(metric_key)
        if metric is None:
            raise refused("metric_invalid", "unknown metric")
        codes = tuple(
            dict.fromkeys(t[1:] for t in metric.numerator + metric.denominator)
        )
        wide = FactQuery(
            subjects=query.subjects,
            codes=codes,
            period_from=(
                query.period_from.replace(year=query.period_from.year - 1)
                if query.period_from and metric.kind == "growth"
                else query.period_from
            ),
            period_to=query.period_to,
            duration=query.duration,
            consolidation=query.consolidation,
            limit=MAX_RESULT_ROWS,
        )
        if run_ref is not None:
            assert executor_app and reader and beneficiary
            rows = await self.query_facts(
                executor_app, reader, beneficiary, run_ref, wide
            )
        else:
            assert subject is not None
            rows = await self.query_market(subject, wide)
        results = compute_metric(metric, rows)
        if query.period_from is not None:
            results = [r for r in results if r.period_end >= query.period_from]
        return results[: query.limit]

    async def renditions(
        self, subject: str, material_id: str, version_no: int
    ) -> list[Rendition]:
        """一版的读法齐不齐（web端详情第二层）；有使用权、那一版在使用权里的才看得到。"""
        holdings = await self.library.holdings_of(subject, material_id)
        if not any(h.covers(version_no) for h in holdings):
            raise not_found()
        version = await self.library.version(material_id, version_no)
        if version is None:
            raise not_found("version")
        return await self.runs.renditions(version.id)

    # —— 产出区 ——

    async def write_output(
        self,
        caller: str,
        beneficiary: str,
        task_ref: str,
        run_ref: str,
        name: str,
        content: bytes,
        *,
        evidence: bool = False,
        provenance: dict[str, object] | None = None,
    ) -> tuple[Material, ContentVersion]:
        """应用替用户写产出：只能写产出区，按（应用，任务，运行，名字）幂等，算用户空间。

        同一次运行同名的再写一次：内容一样回原来那份，不一样 409（测试证据只增不改）。
        """
        if caller not in OUTPUT_CALLERS:
            raise denied("service_scope_denied", "this app does not write outputs")
        check_ref(task_ref)
        check_ref(run_ref)
        filename = clean_name(name)
        found = await self.runs.find_output(
            beneficiary, caller, task_ref, run_ref, filename
        )
        if found is not None:
            _, version = found
            if version.sha256 != hashlib.sha256(content).hexdigest():
                raise conflict("output_exists", "this run already wrote another one")
            material = await self.library.get_material(version.material_id)
            assert material is not None
            return material, version
        media_type = kind_of(filename, content)
        check_size(len(content), await self.library.bytes_used(beneficiary))
        origin = await self._provenance(provenance or {})
        material_id = self.new_id()
        now = self.now()
        key = version_object_key(self.prefix, beneficiary, material_id, 1)
        await self.objects.put(key, content, media_type)
        version = ContentVersion(
            id=self.new_id(),
            material_id=material_id,
            version_no=1,
            sha256=hashlib.sha256(content).hexdigest(),
            size_bytes=len(content),
            media_type=media_type,
            object_key=key,
            created_at=now,
            created_by=caller,
        )
        material = Material(
            id=material_id,
            owner=beneficiary,
            name=filename,
            content_origin="output",
            license="self",
            description=Description(
                data_format=format_of_media(media_type), vocab_version=VOCAB_VERSION
            ),
            current_version=1,
            created_at=now,
            provenance={
                "caller": caller,
                "task_ref": task_ref,
                "run_ref": run_ref,
                "evidence": evidence,
                **origin,
            },
        )
        holding = Holding(
            id=self.new_id(),
            subject=beneficiary,
            material_id=material_id,
            basis="output",
            license="self",
            created_at=now,
        )
        output = Output(
            material_id=material_id,
            owner=beneficiary,
            caller=caller,
            task_ref=task_ref,
            run_ref=run_ref,
            name=filename,
            evidence=evidence,
            created_at=now,
        )
        try:
            saved = await self.runs.add_output(material, version, holding, output)
        except UserDataError as exc:
            if exc.code != "output_exists":
                raise
            # 并发的另一次先写上了：按幂等的规矩再判一次
            await self.objects.delete(key)
            return await self.write_output(
                caller,
                beneficiary,
                task_ref,
                run_ref,
                name,
                content,
                evidence=evidence,
                provenance=provenance,
            )
        return saved, version

    async def convert(
        self,
        subject: str,
        material_id: str,
        *,
        name: str | None = None,
        license_: str = "self",
        attest: bool = False,
    ) -> tuple[Material, ContentVersion]:
        """本人把一份产出转为数据：新建一份资料（产出转来，带出处），进第一区、能被解析。

        许可默认「只许本人用」；上游有受限数据的不得高于上游；改高要勾「我有权」。
        """
        source = await self.library.get_material(material_id)
        if (
            source is None
            or source.owner != subject
            or not source.live
            or not source.in_output_zone
        ):
            raise not_found()
        target: License = check_license(license_)
        cap = await self._upstream_cap(subject, source)
        if license_rank(target) > license_rank(cap):
            raise refused("license_above_upstream", f"upstream allows at most {cap}")
        if license_rank(target) > license_rank("self") and not attest:
            raise refused("attest_required", "raising the license needs an attestation")
        old = await self.library.version(source.id, source.current_version)
        if old is None:
            raise not_found("version")
        content = await self._fetch(old.object_key, old.sha256)
        check_size(len(content), await self.library.bytes_used(subject))
        new_id = self.new_id()
        now = self.now()
        key = version_object_key(self.prefix, subject, new_id, 1)
        await self.objects.put(key, content, old.media_type)
        version = ContentVersion(
            id=self.new_id(),
            material_id=new_id,
            version_no=1,
            sha256=old.sha256,
            size_bytes=old.size_bytes,
            media_type=old.media_type,
            object_key=key,
            created_at=now,
            created_by=subject,
        )
        material = Material(
            id=new_id,
            owner=subject,
            name=clean_name(name or source.name),
            content_origin="output",
            license=target,
            description=source.description,
            current_version=1,
            created_at=now,
            provenance={
                "converted_from": source.id,
                "converted_version": old.version_no,
                "source": dict(source.provenance or {}),
            },
        )
        holding = Holding(
            id=self.new_id(),
            subject=subject,
            material_id=new_id,
            basis="convert",
            license=target,
            created_at=now,
        )
        saved = await self.library.add_material(material, version, holding)
        return saved, version

    # —— 禁用、清除 ——

    async def disable(self, actor: str, material_id: str, reason: str) -> None:
        """禁用（违规处置）：停一切新读取，在跑的授权也撤；留删除事件。"""
        material = await self.library.get_material(material_id)
        if material is None:
            raise not_found()
        _check_reason(reason)
        now = self.now()
        await self.runs.disable(material.id, now)
        ids = await self.runs.grant_ids(material_id=material.id, active_at=now)
        await self.runs.revoke(ids, now, "disabled")
        await self.runs.add_deletion(
            deletion_id=self.new_id(),
            subject=material.owner,
            material_id=material.id,
            action="disable",
            reason=reason,
            actor=actor,
            at=now,
            pending=[],
        )

    async def deletions_of_material(self, material_id: str) -> list[dict[str, object]]:
        """管理端看一份资料的删除事件（禁用、清除、撤关系），按时间。"""
        material = await self.library.get_material(material_id)
        if material is None:
            raise not_found()
        return [
            e
            for e in await self.runs.deletions_of(material.owner)
            if e["material_id"] == material.id
        ]

    async def purge_material(
        self, actor: str, material_id: str, reason: str, *, personal_info: bool
    ) -> bool:
        """依法清除一份：没有别人的使用权，或内容本身是必须删的个人信息时真删。

        别人还合法用着、又不是必须删的个人信息的，不删内容（只撤所有者的关系），
        返回 False。真删：所有版本的原件对象，库里的读法内容（正文、按页文字、事实行），
        描述与名字；撤全部授权；买家的使用权
        结束（通知、补偿买家在 knowmart）。RAGFlow、knowfeed 的回执到之前，事件是
        「处理中」。
        """
        material = await self.library.get_material(material_id)
        if material is None or material.purged_at is not None:
            raise not_found()
        _check_reason(reason)
        now = self.now()
        if not personal_info and await self.runs.other_holders(
            material.id, material.owner
        ):
            if material.deleted_at is None:
                await self.library.remove(material.id, material.owner, now)
            return False
        for version in await self.library.versions(material.id):
            await self.objects.delete(version.object_key)
        await self.runs.mark_purged(material.id, now)
        ids = await self.runs.grant_ids(material_id=material.id, active_at=now)
        await self.runs.revoke(ids, now, "purged")
        await self.runs.add_deletion(
            deletion_id=self.new_id(),
            subject=material.owner,
            material_id=material.id,
            action="purge",
            reason=reason,
            actor=actor,
            at=now,
            pending=list(PURGE_PENDING),
        )
        return True

    async def purge_subject(self, actor: str, subject: str, reason: str) -> int:
        """注销：他名下的逐份清除（别人合法用着的不删内容，卖家注销买家不受影响）；
        他的使用权、同意依据结束，以他为受益人的授权撤。返回真删了几份。"""
        _check_reason(reason)
        purged = 0
        for material_id in await self.runs.owned_material_ids(subject):
            if await self.purge_material(
                actor, material_id, reason, personal_info=False
            ):
                purged += 1
        now = self.now()
        await self.runs.end_holdings(subject, now, "account-closed")
        await self.runs.withdraw_all_uses(subject, now)
        ids = await self.runs.grant_ids(beneficiary=subject, active_at=now)
        await self.runs.revoke(ids, now, "account-closed")
        await self.runs.add_deletion(
            deletion_id=self.new_id(),
            subject=subject,
            material_id=None,
            action="relation",
            reason=reason,
            actor=actor,
            at=now,
            pending=[],
        )
        return purged

    async def deletions(self, subject: str) -> list[dict[str, object]]:
        return await self.runs.deletions_of(subject)

    # —— 内部 ——

    async def _issue(
        self,
        process: str,
        *,
        executor_app: str,
        beneficiary: str,
        issued_by: str,
        ref: str,
        items: list[GrantRequestItem],
        reader_role: str,
        ttl_seconds: int | None,
        appendable: bool,
        platform_use: str | None = None,
        platform_kind: str | None = None,
    ) -> RunGrant:
        """逐项核：版本与摘要、读法与摘要、权利依据、内容所有者；全过才签。"""
        check_ref(ref)
        if not items:
            raise refused("items_invalid", "give at least one item")
        pins = check_pins(tuple(i.pin for i in items))
        granted: dict[tuple[str, int], GrantItem] = {}
        for request in items:
            if request.basis not in BASES_OF[process]:
                raise denied(
                    "basis_not_allowed", f"a {process} grant cannot rely on this basis"
                )
            item = await self._check_item(process, beneficiary, request)
            key = (item.material_id, item.version_no)
            if key in granted and granted[key] != item:
                raise refused("basis_conflict", "one version, one basis")
            granted[key] = item
        now = self.now()
        input_set = await self.runs.save_input_set(
            InputSet(id=input_set_id(pins), pins=pins, created_at=now)
        )
        return await self.runs.add_grant(
            RunGrant(
                id=self.new_id(),
                process=process,  # type: ignore[arg-type]
                executor_app=executor_app,
                beneficiary=beneficiary,
                issued_by=issued_by,
                ref=ref,
                revision=0,
                reader_role=reader_role,  # type: ignore[arg-type]
                scope=SCOPE_OF[process],
                input_set_id=input_set.id,
                items=tuple(granted.values()),
                issued_at=now,
                expires_at=grant_expiry(process, now, ttl_seconds, platform_kind),
                platform_use=platform_use,
            ),
            appendable=appendable,
        )

    async def _check_item(
        self, process: str, beneficiary: str, request: GrantRequestItem
    ) -> GrantItem:
        pin = request.pin
        material = await self.library.get_material(pin.material_id)
        if material is None or not material.readable:
            raise not_found()
        if material.in_output_zone:
            raise refused("output_not_input", "convert an output to data first")
        version = await self.library.version(material.id, pin.version_no)
        if version is None:
            raise not_found("version")
        if version.sha256 != pin.sha256:
            raise conflict("sha256_mismatch", "the pinned version does not match")
        if pin.rendition_kind is not None:
            rendition = await self.runs.rendition(
                version.id, pin.rendition_kind, pin.rendition_no or 0
            )
            if rendition is None or rendition.status != "ready":
                raise not_found("rendition")
            if rendition.sha256 != pin.rendition_sha256:
                raise conflict("sha256_mismatch", "the pinned rendition does not match")
        basis, ref = request.basis, request.basis_ref
        if basis == "display":
            # 展示给执行的那个应用；买来的也行（使用权要覆盖这一版）
            if ref != f"app:{DISPLAY_APP_OF[process]}":
                raise refused("basis_ref_invalid", "display basis names the app")
            if not await self.runs.has_use(beneficiary, material.id, ref):
                raise denied("not_shown", f"not shown to {DISPLAY_APP_OF[process]}")
            await self._covers(beneficiary, material.id, pin.version_no)
        elif basis == "frozen_binding":
            # 作者交付时冻结的绑定：内容归作者，受益人是请专家的人
            if not _FROZEN.fullmatch(ref):
                raise refused(
                    "basis_ref_invalid", "a frozen binding is expert:<id>@<v>"
                )
            scope = await self.runs.use_scope(
                material.owner, material.id, ref, pin.version_no
            )
            if scope is None:
                raise denied("not_bound", "this version is not bound to the expert")
            # 给作者以外的人用：交付那一刻要「可以经专家给别人用」以上
            frozen_license = str(scope.get("license") or "self")
            if beneficiary != material.owner and license_rank(
                frozen_license
            ) < license_rank("via_expert"):
                raise denied("license_self_only", "this data serves its author only")
        elif basis == "platform":
            if not await self.runs.has_use(
                beneficiary, material.id, ref, pin.version_no
            ):
                raise denied("outside_consent", "an item is outside the consent")
            await self._covers(beneficiary, material.id, pin.version_no)
        elif basis == "processing":
            if ref != processing_use(version.id) or material.owner != beneficiary:
                raise refused("basis_ref_invalid", "processing names this version")
            # 公开原件的加工依据就是公开原件层本身（0043 第 2.7 节「加工」）；
            # 用户的上传要本人采用过加工
            if material.owner != PUBLIC_OWNER and not await self.runs.has_use(
                material.owner, material.id, ref
            ):
                raise denied("no_processing_consent", "the owner has not consented")
        elif basis == "reference":
            # 第二区引用：他名下有一条可用的引用覆盖这一版（开关关着也能读）
            holdings = await self.library.holdings_of(beneficiary, material.id)
            if material.owner != PUBLIC_OWNER or not any(
                h.basis == "reference" and h.covers(pin.version_no) for h in holdings
            ):
                raise denied("no_reference", "no usable reference to this original")
            ref = "reference"
        elif basis == "public":
            # 公开原件层全市场可读，过资格、权限、平台总开关那道门（0047 第 2.2 节）
            if material.owner != PUBLIC_OWNER:
                raise refused("basis_ref_invalid", "only public originals are public")
            self.gate.check(beneficiary, material)
            ref = PUBLIC_OWNER
        else:
            raise refused("basis_unsupported", "this basis is not available")
        return GrantItem(
            material_id=material.id,
            version_no=pin.version_no,
            basis=basis,
            basis_ref=ref,
            content_owner=material.owner,
        )

    async def _reading_of(
        self, grant: RunGrant, item: GrantItem, kind: str
    ) -> tuple[ContentVersion, Rendition]:
        """授权里这一项要读的那个读法版本：钉了这种读法的就是那一版，没钉的取最新就绪的。"""
        if grant.scope != "content":
            raise denied("format_only", "this grant reads formats only")
        material = await self.library.get_material(item.material_id)
        if material is None or not material.readable:
            raise not_found()
        version = await self.library.version(item.material_id, item.version_no)
        pins = await self._pins(grant, item)
        if version is None or version.sha256 != pins[0].sha256:
            raise conflict("sha256_mismatch", "the pinned version does not match")
        pinned = next((p for p in pins if p.rendition_kind == kind), None)
        if pinned is not None:
            rendition = await self.runs.rendition(
                version.id, kind, pinned.rendition_no or 0
            )
            if rendition is None or rendition.sha256 != pinned.rendition_sha256:
                raise conflict("sha256_mismatch", "the pinned rendition does not match")
            return version, rendition
        ready = [
            r
            for r in await self.runs.renditions(version.id)
            if r.kind == kind and r.status == "ready"
        ]
        if not ready:
            raise not_found("rendition")
        return version, max(ready, key=lambda r: r.rendition_no)

    async def _run_grants(
        self, executor_app: str, beneficiary: str, run_ref: str
    ) -> list[RunGrant]:
        """这件事（执行应用 + ref）下他还有效的授权，含运行中追加的修订。"""
        check_ref(run_ref)
        out = []
        for grant_id in await self.runs.grant_ids(
            executor_app=executor_app, ref=run_ref, active_at=self.now()
        ):
            grant = await self.runs.get_grant(grant_id)
            if grant is not None and grant.beneficiary == beneficiary:
                out.append(grant)
        return out

    async def _covers(self, subject: str, material_id: str, version_no: int) -> None:
        holdings = await self.library.holdings_of(subject, material_id)
        if not any(h.covers(version_no) for h in holdings):
            raise not_found("version")

    async def _pins(self, grant: RunGrant, item: GrantItem) -> list[InputPin]:
        """这一项在组合里的钉子（同一版钉了读法的排在后面）。"""
        input_set = await self.runs.input_set(grant.input_set_id)
        pins = [
            p
            for p in (input_set.pins if input_set else ())
            if p.material_id == item.material_id and p.version_no == item.version_no
        ]
        if not pins:
            raise not_found()
        return sorted(pins, key=lambda p: p.rendition_kind is not None)

    async def _revoke_platform(self, subject: str, use: str, reason: str) -> None:
        ids = await self.runs.grant_ids(
            beneficiary=subject, platform_use=use, active_at=self.now()
        )
        await self.runs.revoke(ids, self.now(), reason)

    async def _fetch(self, key: str, sha256: str | None) -> bytes:
        content = await self.objects.get(key)
        if hashlib.sha256(content).hexdigest() != sha256:
            raise UserDataError(
                503, "storage_mismatch", "stored content does not match"
            )
        return content

    async def _log(
        self,
        grant: RunGrant,
        item: GrantItem,
        version: ContentVersion,
        reader: str,
        operation: str,
    ) -> None:
        await self.runs.record_read(
            owner=item.content_owner,
            material_id=item.material_id,
            sha256=version.sha256,
            reader=reader,
            operation=operation,
            acting_app=grant.executor_app,
            at=self.now(),
            grant_id=grant.id,
            beneficiary=grant.beneficiary,
            reader_role=grant.reader_role,
        )

    async def _provenance(self, given: dict[str, object]) -> dict[str, object]:
        """产出的出处只认几样：哪位专家哪一版、用了哪组输入（组合要真有）。"""
        unknown = set(given) - set(_PROVENANCE_KEYS)
        if unknown:
            raise refused("provenance_invalid", f"unknown keys: {sorted(unknown)}")
        for key in _PROVENANCE_KEYS:
            value = given.get(key)
            if value is not None and (not isinstance(value, str) or len(value) > 255):
                raise refused("provenance_invalid", f"{key} is invalid")
        set_id = given.get("input_set_id")
        if set_id is not None and await self.runs.input_set(str(set_id)) is None:
            raise not_found("input_set")
        return {k: v for k, v in given.items() if v is not None}

    async def _upstream_cap(self, subject: str, source: Material) -> License:
        """上游的许可上限：输入里他自己有使用权的取他那条的档，没有的（作者绑定的）
        只许本人用；没有输入记录的不设上限。"""
        set_id = (source.provenance or {}).get("input_set_id")
        if not set_id:
            return "sale"
        input_set = await self.runs.input_set(str(set_id))
        if input_set is None:
            return "self"
        cap = "sale"
        for pin in input_set.pins:
            holdings = await self.library.holdings_of(subject, pin.material_id)
            held = [h for h in holdings if h.covers(pin.version_no)]
            rank = max((license_rank(h.license) for h in held), default=0)
            if rank < license_rank(cap):
                cap = ("self", "via_expert", "sale")[rank]
        return cap  # type: ignore[return-value]

    async def _own_session(self, subject: str, app: str, session_id: str) -> Session:
        session = await self.runs.get_session(session_id)
        if session is None or session.subject != subject or session.executor_app != app:
            raise not_found("session")
        if not session.active(self.now()):
            raise denied("session_ended", "the session has ended or expired")
        return session

    def _processor(self, caller: str) -> None:
        if caller not in PROCESSORS:
            raise denied("service_scope_denied", "only knowfeed processes uploads")


def _platform_kind(use: str) -> str:
    parts = use.split(":")
    if len(parts) != 4 or parts[0] != "platform" or parts[2] not in PLATFORM_KINDS:
        raise refused(
            "basis_ref_invalid", "platform basis is platform:<app>:<kind>:<ref>"
        )
    platform_use(parts[1], parts[2], parts[3])
    return parts[2]


def _check_reason(reason: str) -> None:
    if not reason or len(reason) > 80:
        raise refused("reason_invalid", "say why, in at most 80 characters")
