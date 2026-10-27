"""资料库：上传、内容版本、描述、许可档、总栏、下载、删、同意依据（SDD 0043 第二节）。

谁能做什么：
- 资料的所有者：传新版本、改描述与许可档、在栏里删；
- 有使用权的人（所有者、买家）：在总栏看到、下载他能用的那一版
  （买家只到交易那一版）。
所有动作都以用户本人的身份（换来的令牌，`sub`）；`actor` 记代办的应用，
读数留痕时写进去。
"""

from __future__ import annotations

import hashlib
import uuid
from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, datetime

from app.application.ports.user_data import UserObjects
from app.application.ports.user_library import UserLibraryRepository
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
    CATEGORY_OF_BASIS,
    COLUMN_USES,
    DEFAULT_DISPLAY_USES,
    Account,
    ContentVersion,
    Description,
    Expert,
    Holding,
    License,
    Material,
    ShelfEntry,
    SubscriptionScope,
    Use,
    check_license,
    check_material_use,
    license_rank,
    platform_use,
    processing_use,
    subscription_use,
    version_object_key,
)
from app.domain.vocabulary import VOCAB_VERSION, format_of_media


class UserLibraryService:
    def __init__(
        self,
        repo: UserLibraryRepository,
        objects: UserObjects,
        *,
        prefix: str,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        new_id: Callable[[], str] = lambda: str(uuid.uuid4()),
    ) -> None:
        self.repo = repo
        self.objects = objects
        self.prefix = prefix
        self.now = clock
        self.new_id = new_id

    # —— 上传与版本 ——

    async def upload(
        self,
        subject: str,
        name: str | None,
        content: bytes,
        *,
        description: Description | None = None,
        confirm: bool = False,
        license_: str = "self",
        process: bool = False,
    ) -> tuple[Material, ContentVersion]:
        """用户上传一份新资料：记资料、第 1 版、所有者的使用权（取得依据 upload）。

        描述是本人填的、勾了确认才算「用户确认过」（参与解析）；没填的格式按内容类型补。
        `process`：本人按页面说明采用加工，就是这一版的加工依据。
        """
        filename = clean_name(name)
        media_type = kind_of(filename, content)
        check_size(len(content), await self.repo.bytes_used(subject))
        license_value = check_license(license_)
        material_id = self.new_id()
        now = self.now()
        version = await self._store(subject, material_id, 1, media_type, content, now)
        material = Material(
            id=material_id,
            owner=subject,
            name=filename,
            content_origin="upload",
            license=license_value,
            description=self._description(description, media_type, confirm),
            current_version=1,
            created_at=now,
        )
        holding = Holding(
            id=self.new_id(),
            subject=subject,
            material_id=material_id,
            basis="upload",
            license=license_value,
            created_at=now,
        )
        saved = await self.repo.add_material(material, version, holding)
        # 账号级「新数据默认展示给我的全部应用」：本人看过点过才算依据
        if (await self.repo.account(subject)).displays_by_default:
            for use in DEFAULT_DISPLAY_USES:
                await self._add_use(subject, saved.id, use, "default-display")
        if process:
            await self._add_processing(subject, version)
        return saved, version

    async def add_version(
        self,
        subject: str,
        material_id: str,
        name: str | None,
        content: bytes,
        *,
        process: bool = False,
    ) -> tuple[Material, ContentVersion]:
        """所有者传这份资料的新内容版本；旧版本照旧留着，钉住旧版的照旧读旧的。"""
        material = await self._owned(subject, material_id)
        media_type = kind_of(clean_name(name or material.name), content)
        check_size(len(content), await self.repo.bytes_used(subject))
        version = await self._store(
            subject,
            material.id,
            material.current_version + 1,
            media_type,
            content,
            self.now(),
        )
        saved = await self.repo.add_version(version)
        if process and saved.content_origin == "upload":
            await self._add_processing(subject, version)
        return saved, version

    # —— 描述、许可档 ——

    async def describe(
        self,
        subject: str,
        material_id: str,
        description: Description,
        *,
        confirm: bool,
    ) -> Material:
        """所有者改描述：整份替换；勾了确认才算用户确认过。改描述不产生新的内容版本。"""
        material = await self._owned(subject, material_id)
        current = await self.repo.version(material.id, material.current_version)
        media_type = current.media_type if current else ""
        updated = self._description(description, media_type, confirm)
        return await self.repo.set_description(
            material.id, updated, subject, self.now()
        )

    async def set_license(
        self, subject: str, material_id: str, license_: str, *, attest: bool
    ) -> Material:
        """所有者改许可档。只有自己上传的能改；改高要本人勾「我有权」，改高改低都留痕。

        改低只影响新的销售与公开用途；买家已有的使用权按交易时的依据，不动。
        """
        material = await self._owned(subject, material_id)
        if material.content_origin != "upload":
            raise refused(
                "license_fixed",
                "the license of collected or derived material is set by the system",
            )
        target: License = check_license(license_)
        if license_rank(target) > license_rank(material.license) and not attest:
            raise refused("attest_required", "raising the license needs an attestation")
        if target == material.license:
            return material
        return await self.repo.set_license(
            material.id, target, subject, self.now(), attest
        )

    # —— 看、下载、删 ——

    async def shelf(
        self, subject: str, category: str | None = None
    ) -> list[ShelfEntry]:
        if category is not None and category not in set(CATEGORY_OF_BASIS.values()):
            raise refused("category_invalid", "category is invalid")
        entries = await self.repo.shelf(subject)
        if category is None:
            return entries
        return [e for e in entries if e.category == category]

    async def get(self, subject: str, material_id: str) -> ShelfEntry:
        """他有使用权的一份：资料、他的使用权、他能看到的那一版。"""
        material, holding = await self._held(subject, material_id)
        number = holding.version_no or material.current_version
        version = await self.repo.version(material.id, number)
        if version is None:
            raise not_found()
        return ShelfEntry(material, holding, version)

    async def versions(self, subject: str, material_id: str) -> list[ContentVersion]:
        """所有者看全部版本；买家只看到交易那一版。"""
        material, holding = await self._held(subject, material_id)
        return [
            v
            for v in await self.repo.versions(material.id)
            if holding.covers(v.version_no)
        ]

    async def history(self, subject: str, material_id: str) -> list[dict[str, object]]:
        material = await self._owned(subject, material_id)
        return await self.repo.history(material.id)

    async def download(
        self,
        subject: str,
        actor: str | None,
        material_id: str,
        version_no: int | None = None,
    ) -> tuple[ContentVersion, bytes]:
        material, holding = await self._held(subject, material_id)
        number = version_no or holding.version_no or material.current_version
        if not holding.covers(number):
            raise not_found("version")
        version = await self.repo.version(material.id, number)
        if version is None:
            raise not_found("version")
        content = await self.objects.get(version.object_key)
        if hashlib.sha256(content).hexdigest() != version.sha256:
            raise UserDataError(
                503, "storage_mismatch", "stored content does not match"
            )
        await self.repo.record_read(
            owner=material.owner,
            material_id=material.id,
            sha256=version.sha256,
            reader=subject,
            operation="owner-download",
            acting_app=actor,
            at=self.now(),
        )
        return version, content

    async def remove(self, subject: str, material_id: str) -> None:
        """所有者在栏里删：逻辑删，内容不删；买家的使用权、钉住的测试与专家版本照旧可读。"""
        material = await self._owned(subject, material_id)
        await self.repo.remove(material.id, subject, self.now())

    # —— 展示与绑定（同意依据，块 1b） ——

    async def show(self, subject: str, material_id: str, use: str) -> Use:
        """本人点展示按钮：展示给某个应用、交给 knowmart、绑给名下专家。

        展示给应用、绑给专家：有使用权的都能点（买来的也能给自己的应用、自用专家用）；
        交给 knowmart：只有所有者能点（上架时 knowmart 再核许可）。
        """
        checked = check_material_use(use)
        material, _ = await self._held(subject, material_id)
        if checked == "market" and material.owner != subject:
            raise refused("not_owner", "only the owner can hand material to knowmart")
        if checked.startswith("expert:"):
            mine = {e.expert_id for e in await self.repo.experts_of(subject)}
            if checked.removeprefix("expert:") not in mine:
                raise not_found("expert")
        return await self._add_use(subject, material.id, checked, "user")

    async def hide(self, subject: str, material_id: str, use: str) -> None:
        """撤展示：停新的解析与授权申请；已签的到期或任务结束为止。

        取消「绑给专家」只影响下一版，已冻结的 `expert:<专家>@<版本>` 不撤。
        """
        checked = check_material_use(use)
        material, _ = await self._held(subject, material_id)
        await self.repo.withdraw_use(subject, material.id, checked, self.now())

    async def show_many(
        self, subject: str, material_ids: list[str], use: str, *, on: bool
    ) -> int:
        """批量勾或批量撤；返回处理了几份（没有使用权的跳过）。"""
        done = 0
        for material_id in dict.fromkeys(material_ids):
            try:
                if on:
                    await self.show(subject, material_id, use)
                else:
                    await self.hide(subject, material_id, use)
            except UserDataError as exc:
                if exc.status != 404:
                    raise
                continue
            done += 1
        return done

    async def uses(self, subject: str, material_id: str) -> list[Use]:
        material, _ = await self._held(subject, material_id)
        return await self.repo.uses_of(subject, material.id)

    async def column(self, subject: str, app: str) -> list[ShelfEntry]:
        """某个应用的知识库栏：这个用户展示给这个应用、自己还有使用权的资料。"""
        use = COLUMN_USES.get(app)
        if use is None:
            raise refused("column_invalid", "no such column")
        shown = await self.repo.material_ids_with_use(subject, use)
        return [e for e in await self.repo.shelf(subject) if e.material.id in shown]

    # —— 平台过程、订阅、加工（块 1b 余下） ——

    async def consent_platform(
        self,
        subject: str,
        app: str,
        kind: str,
        ref: str,
        items: list[tuple[str, int | None]],
    ) -> list[Use]:
        """本人在 knowlab 选平台、交审、提交交付时：这次过程能看哪几份，每份一行。

        范围是这次过程：用途里带过程编号，过程结束、撤回、不受理时整个撤。重跑要读的
        冻结输入带版本号，不带的取当前版本（买来的取成交那一版）；那一版要在本人的
        使用权里。
        """
        use = platform_use(app, kind, ref)
        if not items or len(items) > 200:
            raise refused("items_invalid", "give 1 to 200 items")
        made: list[Use] = []
        for material_id, version_no in dict.fromkeys(items):
            material, holding = await self._held(subject, material_id)
            wanted = version_no or holding.version_no or material.current_version
            if not holding.covers(wanted) or (
                await self.repo.version(material.id, wanted) is None
            ):
                raise not_found("version")
            made.append(
                await self.repo.add_use(
                    Use(
                        id=self.new_id(),
                        subject=subject,
                        use=use,
                        created_at=self.now(),
                        created_via=f"user:{app}",
                        material_id=material.id,
                        version_no=wanted,
                        scope={"kind": kind, "ref": ref},
                    )
                )
            )
        return made

    async def platform_consents(
        self, subject: str, app: str, kind: str, ref: str
    ) -> list[Use]:
        use = platform_use(app, kind, ref)
        return [u for u in await self.repo.named(subject, use) if u.use == use]

    async def withdraw_platform(
        self, subject: str, app: str, kind: str, ref: str
    ) -> int:
        """本人撤回这次平台过程的依据（每份一并撤）；块 1c 起同时撤相关授权。"""
        return await self.repo.withdraw_named(
            subject, platform_use(app, kind, ref), self.now()
        )

    async def subscribe(
        self,
        subject: str,
        subscription_id: str,
        revision: int,
        scope: SubscriptionScope,
        via: str,
    ) -> Use:
        """建订阅、续订时本人留下的长期范围同意；一个修订定了就不改，改范围出新修订。"""
        use = subscription_use(subscription_id, revision)
        wanted = scope.checked().as_dict()
        saved = await self.repo.add_use(
            Use(
                id=self.new_id(),
                subject=subject,
                use=use,
                created_at=self.now(),
                created_via=via,
                scope=wanted,
            )
        )
        if saved.scope != wanted:
            raise conflict(
                "subscription_revision_exists",
                "this revision already has another scope; renew as a new revision",
            )
        return saved

    async def unsubscribe(
        self, subject: str, subscription_id: str, revision: int
    ) -> int:
        return await self.repo.withdraw_named(
            subject, subscription_use(subscription_id, revision), self.now()
        )

    async def subscriptions(self, subject: str) -> list[Use]:
        return await self.repo.named(subject, "subscription:")

    async def consent_processing(
        self, subject: str, material_id: str, version_no: int
    ) -> Use:
        """本人对已传的一版补采用加工（上传时没勾的）；只限上传的、只这一版。"""
        material = await self._owned(subject, material_id)
        if material.content_origin != "upload":
            raise refused(
                "not_upload", "only uploaded material is processed on consent"
            )
        version = await self.repo.version(material.id, version_no)
        if version is None:
            raise not_found("version")
        return await self._add_processing(subject, version)

    # —— 账号设置 ——

    async def account(self, subject: str) -> Account:
        return await self.repo.account(subject)

    async def acknowledge_default_display(
        self, subject: str, default_display: bool
    ) -> Account:
        """第一次进 knowbase web端时把开关亮出来，本人点一下（开或关）就是依据。"""
        account = Account(
            subject=subject,
            default_display=default_display,
            default_display_seen_at=self.now(),
        )
        return await self.repo.save_account(account, self.now())

    # —— 名下专家（knowlab 推来） ——

    async def upsert_expert(
        self, expert_id: str, owner: str, name: str, latest_version: str | None
    ) -> Expert:
        if not expert_id or len(expert_id) > 64 or not name or len(name) > 200:
            raise refused("expert_invalid", "expert id or name is invalid")
        return await self.repo.upsert_expert(
            Expert(
                expert_id=expert_id,
                owner=owner,
                name=name,
                latest_version=latest_version,
                updated_at=self.now(),
            )
        )

    async def experts(self, subject: str) -> list[Expert]:
        return await self.repo.experts_of(subject)

    # —— knowmart 成交：给买家加使用权（不复制） ——

    async def purchase(
        self,
        seller: str,
        material_id: str,
        buyer: str,
        transaction_ref: str,
        *,
        resale_allowed: bool = False,
    ) -> Holding:
        """成交后给买家建使用权，只到成交那一个内容版本；同一笔交易重试只建一条。

        卖的资料要「可以出售」；买家的许可继承卖家的转卖设置，不得升档：不许转卖的
        只许本人用。卖家后来删、下架、改低许可，都不影响这条使用权。
        """
        material = await self.repo.get_material(material_id)
        if material is None or material.owner != seller or not material.live:
            raise not_found()
        if material.license != "sale":
            raise refused("not_for_sale", "this material is not licensed for sale")
        if not transaction_ref or len(transaction_ref) > 255:
            raise refused("transaction_ref_invalid", "transaction_ref is invalid")
        return await self.repo.add_holding(
            Holding(
                id=self.new_id(),
                subject=buyer,
                material_id=material.id,
                basis="purchase",
                license="sale" if resale_allowed else "self",
                created_at=self.now(),
                version_no=material.current_version,
                ref=transaction_ref,
            )
        )

    # —— 内部 ——

    async def _add_use(self, subject: str, material_id: str, use: str, via: str) -> Use:
        return await self.repo.add_use(
            Use(
                id=self.new_id(),
                subject=subject,
                use=use,
                created_at=self.now(),
                created_via=via,
                material_id=material_id,
            )
        )

    async def _add_processing(self, subject: str, version: ContentVersion) -> Use:
        return await self.repo.add_use(
            Use(
                id=self.new_id(),
                subject=subject,
                use=processing_use(version.id),
                created_at=self.now(),
                created_via="user",
                material_id=version.material_id,
                version_no=version.version_no,
            )
        )

    async def _store(
        self,
        subject: str,
        material_id: str,
        version_no: int,
        media_type: str,
        content: bytes,
        now: datetime,
    ) -> ContentVersion:
        key = version_object_key(self.prefix, subject, material_id, version_no)
        # 先存对象再记：记没写上，只多一个没人引用的对象
        await self.objects.put(key, content, media_type)
        return ContentVersion(
            id=self.new_id(),
            material_id=material_id,
            version_no=version_no,
            sha256=hashlib.sha256(content).hexdigest(),
            size_bytes=len(content),
            media_type=media_type,
            object_key=key,
            created_at=now,
            created_by=subject,
        )

    @staticmethod
    def _description(
        given: Description | None, media_type: str, confirm: bool
    ) -> Description:
        base = given or Description()
        filled = replace(
            base,
            data_format=base.data_format or format_of_media(media_type),
            status="confirmed" if confirm else "unconfirmed",
            vocab_version=VOCAB_VERSION,
        )
        return filled.checked()

    async def _owned(self, subject: str, material_id: str) -> Material:
        material = await self.repo.get_material(material_id)
        if material is None or material.owner != subject or not material.live:
            raise not_found()
        return material

    async def _held(self, subject: str, material_id: str) -> tuple[Material, Holding]:
        material = await self.repo.get_material(material_id)
        if material is None or material.disabled_at is not None:
            raise not_found()
        holdings = await self.repo.holdings_of(subject, material.id)
        if not holdings:
            raise not_found()
        # 所有者的那条（不限版本）优先；没有就是买来的
        holding = next((h for h in holdings if h.version_no is None), holdings[0])
        return material, holding
