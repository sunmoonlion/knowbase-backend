"""运行：运行授权（第四版）、凭授权读、加工写回读法、产出区、禁用与清除。

SDD 0043 块 1c。

谁能做什么：
- 用户本人（应用凭本人令牌转交）：签委托、会话、孵化的授权；撤自己的授权；
  把产出转为数据；看自己资料的读数留痕；撤回平台过程的依据（授权一并撤）。
- knowlab（服务身份）：凭展示依据签测试授权，凭 `platform:` 依据签平台过程授权
  （运营、机器分开签）；过程结束、不受理时撤。
- knowbench、knowlab（服务身份）：替用户往产出区写，按（任务，运行，名字）幂等。
- knowfeed（服务身份）：凭本人的加工依据领加工授权，读那一版、写回读法。
- 执行的应用：凭授权读钉住的那一版（或那个读法），用完交还。
- 管理端：禁用（违规处置）、注销与依法删除。
"""

from __future__ import annotations

import hashlib
import re
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime

from app.application.ports.user_data import UserObjects
from app.application.ports.user_library import UserLibraryRepository
from app.application.ports.user_runs import UserRunRepository
from app.application.services.user_data_service import FormatView, format_view
from app.domain.user_data import (
    UserDataError,
    check_size,
    clean_name,
    conflict,
    kind_of,
    not_found,
    refused,
)
from app.domain.user_library import (
    PLATFORM_KINDS,
    ContentVersion,
    Description,
    Holding,
    License,
    Material,
    Rendition,
    check_license,
    license_rank,
    platform_use,
    processing_use,
    version_object_key,
)
from app.domain.user_runs import (
    BASES_OF,
    DISPLAY_APP_OF,
    SCOPE_OF,
    SESSION_HINT_AT,
    USER_PROCESSES,
    GrantItem,
    GrantRequestItem,
    InputPin,
    InputSet,
    Output,
    RunGrant,
    check_pins,
    check_ref,
    denied,
    grant_expiry,
    input_set_id,
)
from app.domain.vocabulary import VOCAB_VERSION, format_of_media

OUTPUT_CALLERS = ("knowbench", "knowlab")
PROCESSORS = ("knowfeed",)
# knowfeed 写回的读法；扫描版与检索索引由 knowbase 经 RAGFlow 做（块 9）
PROCESSOR_KINDS = ("text", "pages", "table")
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
    ) -> None:
        self.library = library
        self.runs = runs
        self.objects = objects
        self.prefix = prefix
        self.now = clock
        self.new_id = new_id

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

    async def session_reads(
        self,
        subject: str,
        app: str,
        session_ref: str,
        items: list[GrantRequestItem],
    ) -> tuple[list[RunGrant], int, bool]:
        """会话里每首读一份签一条小授权（一份一条，可以一次签多条），会话结束一并撤。

        返回这次签的、这段会话累计签了几份、要不要提示（累计刚过 50 份时提示一次，
        不挡）。
        """
        if not items or len(items) > 20:
            raise refused("items_invalid", "give 1 to 20 items")
        made = [
            await self.grant_by_user(subject, app, "session", session_ref, [item])
            for item in items
        ]
        total = len(await self.runs.grant_ids(executor_app=app, ref=session_ref))
        before = total - len(made)
        return made, total, before < SESSION_HINT_AT <= total

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
    ) -> RunGrant:
        """knowlab 签：测试凭展示依据（固定测试集，不许中途追加）；平台过程凭
        `platform:` 依据，读的一方分运营与机器，上限按过程配。"""
        if caller != "knowlab":
            raise denied("service_scope_denied", "only knowlab signs these grants")
        if process == "test":
            if reader_role != "app":
                raise refused("reader_role_invalid", "a test is read by the app")
            return await self._issue(
                "test",
                executor_app="knowlab",
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
        uses = {i.basis_ref for i in items}
        if len(uses) != 1:
            raise refused("basis_ref_invalid", "one platform process per grant")
        use = uses.pop()
        kind = _platform_kind(use)
        return await self._issue(
            "platform",
            executor_app="knowlab",
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
            content = await self._fetch(rendition.object_key or "", rendition.sha256)
            await self._log(grant, item, version, reader, operation)
            return ReadResult(
                item, version, "application/octet-stream", content, rendition=rendition
            )
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
    ) -> Rendition:
        """knowfeed 写回读法：只能凭加工授权、只能写那一版。

        没成的不带内容、写明原因。
        """
        self._processor(caller)
        if kind not in PROCESSOR_KINDS:
            raise refused("kind_invalid", "knowfeed writes text, pages or table")
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
        sha256 = key = None
        if content is not None:
            if not content:
                raise refused("empty", "rendition is empty")
            key = f"{version.object_key}/renditions/{kind}/{self.new_id()}"
            await self.objects.put(key, content, "application/octet-stream")
            sha256 = hashlib.sha256(content).hexdigest()
        elif not (detail or {}).get("reason"):
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
                object_key=key,
                detail=dict(detail or {}),
                ready_at=now if content is not None else None,
            )
        )

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

    async def purge_material(
        self, actor: str, material_id: str, reason: str, *, personal_info: bool
    ) -> bool:
        """依法清除一份：没有别人的使用权，或内容本身是必须删的个人信息时真删。

        别人还合法用着、又不是必须删的个人信息的，不删内容（只撤所有者的关系），
        返回 False。真删：所有版本与读法的对象、描述与名字；撤全部授权；买家的使用权
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
            for rendition in await self.runs.renditions(version.id):
                if rendition.object_key:
                    await self.objects.delete(rendition.object_key)
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
            if not await self.runs.has_use(material.owner, material.id, ref):
                raise denied("no_processing_consent", "the owner has not consented")
        else:
            # 第二区引用的存法待定（待你决定第 1 条）
            raise refused("basis_unsupported", "this basis is not available yet")
        return GrantItem(
            material_id=material.id,
            version_no=pin.version_no,
            basis=basis,
            basis_ref=ref,
            content_owner=material.owner,
        )

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
