"""资料库：上传、内容版本、描述、许可档、总栏、下载、删、同意依据（SDD 0043 第二节）。

谁能做什么：
- 资料的所有者：传新版本、改描述与许可档、在栏里删；
- 有使用权的人（所有者、买家）：在总栏看到、下载他能用的那一版
  （买家只到交易那一版）。
所有动作都以用户本人的身份（换来的令牌，`sub`）；`actor` 记代办的应用，
读数留痕时写进去。
"""

from __future__ import annotations

import asyncio
import hashlib
import re
import uuid
from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, datetime

from app.application.ports.user_data import FirstPageReader, UserObjects
from app.application.ports.user_library import UserLibraryRepository
from app.domain.recognition import Recognition, recognize
from app.domain.user_data import (
    MAX_ITEM_BYTES,
    QUOTA_BYTES,
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
    DIRECT_UPLOAD_APPS,
    PUBLIC_OWNER,
    Account,
    ContentVersion,
    Description,
    Expert,
    Holding,
    License,
    LogEntry,
    Material,
    ShelfEntry,
    SubscriptionScope,
    Use,
    check_license,
    check_material_use,
    frozen_expert_use,
    license_rank,
    platform_use,
    processing_use,
    subscription_use,
    version_object_key,
)
from app.domain.vocabulary import VOCAB_VERSION, format_of_media

# 公开原件不算谁的空间；招股书这类比用户上传的上限（50 MB）大
MAX_PUBLIC_BYTES = 200 * 1024 * 1024
_CHANNEL = re.compile(r"^[a-z][a-z0-9_-]{1,39}$")
_CHANNEL_DOC = re.compile(r"^[A-Za-z0-9._:/-]{1,200}$")


def denied_scope() -> UserDataError:
    return UserDataError(
        403, "service_scope_denied", "only knowfeed delivers originals"
    )


class UserLibraryService:
    def __init__(
        self,
        repo: UserLibraryRepository,
        objects: UserObjects,
        *,
        prefix: str,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        new_id: Callable[[], str] = lambda: str(uuid.uuid4()),
        first_page: FirstPageReader | None = None,
    ) -> None:
        self.repo = repo
        self.objects = objects
        self.prefix = prefix
        self.now = clock
        self.new_id = new_id
        self.first_page = first_page

    # —— 上传与版本 ——

    def recognize(self, name: str | None, content: bytes) -> Recognition:
        """选文件后先识别描述（文件名、首页文字、表头），交本人确认或改
        （0044 第 2.3 节）。

        只读不存：文件不落盘，读出来的字不留。
        """
        filename = clean_name(name)
        media_type = kind_of(filename, content)
        if not content:
            raise refused("empty", "file is empty")
        if len(content) > MAX_ITEM_BYTES:
            raise UserDataError(413, "too_large", "file is larger than 50 MB")
        text = self.first_page(media_type, content) if self.first_page else ""
        return recognize(filename, text, format_of_media(media_type))

    async def usage(self, subject: str) -> tuple[int, int]:
        """空间用量：已用多少、上限多少（1 GB，0044 第 2.1 节）。"""
        return await self.repo.bytes_used(subject), QUOTA_BYTES

    async def upload(
        self,
        subject: str,
        name: str | None,
        content: bytes,
        *,
        description: Description | None = None,
        confirm: bool = False,
        license_: str = "self",
        attest: bool = False,
        process: bool = False,
        display_to: str | None = None,
    ) -> tuple[Material, ContentVersion]:
        """用户上传一份新资料：记资料、第 1 版、所有者的使用权（取得依据 upload）。

        描述是本人填的、勾了确认才算「用户确认过」（参与解析）；没填的格式按内容类型补。
        许可档选高于「只许本人用」的要本人勾「我有权」，和改高一样留痕（0044 第 2.2、
        2.3 节）。上传时按文件名与首页识别一次，把用的规则版本与认出的值记进留痕
        （0049 第六节）。`process`：本人按页面说明采用加工，就是这一版的加工依据。

        `display_to`：直达上传的来源应用（0044 第 2.4 节）。本人在 knowbase 页面上传就是
        依据，「展示给来源应用」和资料在同一个事务里记下，应用不能事后调接口声明。
        """
        filename = clean_name(name)
        media_type = kind_of(filename, content)
        check_size(len(content), await self.repo.bytes_used(subject))
        license_value = check_license(license_)
        if license_rank(license_value) > license_rank("self") and not attest:
            raise refused("attest_required", "raising the license needs an attestation")
        if display_to is not None and display_to not in DIRECT_UPLOAD_APPS:
            raise refused("display_app_invalid", "no column in this app")
        # 读 PDF 首页是算力活，放到线程里
        recognition = await asyncio.to_thread(self.recognize, filename, content)
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
        # 账号级「新数据默认展示给我的全部应用」：本人看过点过才算依据
        shown: dict[str, str] = {}
        if (await self.repo.account(subject)).displays_by_default:
            shown = dict.fromkeys(DEFAULT_DISPLAY_USES, "default-display")
        if display_to is not None:
            shown[f"app:{display_to}"] = "direct-upload"
        logs = [
            LogEntry(
                "recognized",
                {},
                {**recognition.as_log(), "confirmed": confirm},
            )
        ]
        if license_rank(license_value) > license_rank("self"):
            logs.append(
                LogEntry(
                    "license",
                    {"license": None},
                    {"license": license_value, "attested": attest, "at": "upload"},
                )
            )
        saved = await self.repo.add_material(
            material,
            version,
            holding,
            uses=[
                Use(
                    id=self.new_id(),
                    subject=subject,
                    use=use,
                    created_at=now,
                    created_via=via,
                    material_id=material_id,
                )
                for use, via in shown.items()
            ],
            logs=logs,
        )
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
        """所有者传这份资料的新内容版本；旧版本照旧留着，钉住旧版的照旧读旧的。

        产出区的不改（测试证据只增不改）；要改先转为数据。
        """
        material = await self._owned(subject, material_id)
        if material.in_output_zone:
            raise refused("output_immutable", "outputs are not revised")
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

    async def shown(self, subject: str) -> dict[str, list[str]]:
        """总栏里每份资料展示到哪里（几个小标签，0044 第 2.1 节）：资料 → 用途。"""
        return await self.repo.live_uses(subject)

    # —— knowmart 成批核条目里的资料（后台定时下架用） ——

    async def offer_states(
        self, caller: str, items: list[tuple[str, str]]
    ) -> list[tuple[str, str, str]]:
        """（卖家, 资料）→ offered / not_for_sale / gone。只读，只认 knowmart。

        offered：是这位卖家的、还在、还交给着 knowmart、许可「可以出售」；
        not_for_sale：还在、还交给着 knowmart，许可改低了；其余（没有、不是这位卖家的、
        删了、禁用了、不再交给 knowmart）都是 gone。不是谁的与没有的一样，不多说。
        """
        if caller != "knowmart":
            raise UserDataError(
                403, "service_scope_denied", "only knowmart checks market offers"
            )
        if not 1 <= len(items) <= 200:
            raise refused("items_invalid", "give 1 to 200 items")
        handed: dict[str, set[str]] = {}
        states: list[tuple[str, str, str]] = []
        for seller, material_id in items:
            material = await self.repo.get_material(material_id)
            if material is None or material.owner != seller or not material.live:
                states.append((seller, material_id, "gone"))
                continue
            if seller not in handed:
                handed[seller] = await self.repo.material_ids_with_use(seller, "market")
            if material.id not in handed[seller]:
                state = "gone"
            elif material.license != "sale":
                state = "not_for_sale"
            else:
                state = "offered"
            states.append((seller, material_id, state))
        return states

    async def binding_states(
        self, caller: str, items: list[tuple[str, str, int, str | None]]
    ) -> list[tuple[str, str, int, str, str | None, bool, str | None]]:
        """专家所绑的每份（作者, 资料, 版本）现在怎样，只读（0043 第 4.3 节
        「两种许可检查」）。

        knowmart 收清单时硬查、后台定时核在售专家；knowlab 运营审核门预查。
        回（作者, 资料, 版本, 状态, 许可, 能不能经专家给别人用）。状态：
        live 在；deleted 删了（作者删、卖家删都算，只影响新的公开用途）；
        disabled 禁用；gone 没有、作者没有这一版的使用权。许可：自己的是资料
        现在的许可档，买来的是成交时定的那一档（不得升档）。不是作者的与
        没有的一样，不多说。带了 `basis_ref`（`expert:<专家>@<版本>`）的再回冻结时的
        许可：能不能给别人用还要冻结的那一档够（签授权看的是它）。
        """
        if caller not in ("knowmart", "knowlab"):
            raise UserDataError(
                403, "service_scope_denied", "only knowmart and knowlab check bindings"
            )
        if not 1 <= len(items) <= 200:
            raise refused("items_invalid", "give 1 to 200 items")
        out: list[tuple[str, str, int, str, str | None, bool, str | None]] = []
        for owner, material_id, version_no, basis_ref in items:
            material = await self.repo.get_material(material_id)
            if (
                material is None
                or material.purged_at is not None
                or await self.repo.version(material.id, version_no) is None
            ):
                out.append((owner, material_id, version_no, "gone", None, False, None))
                continue
            if material.owner == owner:
                # 自己的：删了也还是这个人的那一份，照实说「删了」
                license_: str = material.license
            else:
                holding = next(
                    (
                        h
                        for h in await self.repo.holdings_of(owner, material.id)
                        if h.active and h.covers(version_no)
                    ),
                    None,
                )
                if holding is None:
                    out.append(
                        (owner, material_id, version_no, "gone", None, False, None)
                    )
                    continue
                license_ = holding.license
            state = (
                "disabled"
                if material.disabled_at is not None
                else "deleted"
                if material.deleted_at is not None
                else "live"
            )
            frozen_license: str | None = None
            if basis_ref is not None:
                # 冻结了的：签请专家的授权看冻结时记下的许可（0043 第 2.2 节），
                # 给别人用要冻结的那一档也够；这一版没冻结这一份就是没绑
                scope = next(
                    (
                        u.scope
                        for u in await self.repo.named(owner, basis_ref)
                        if u.use == basis_ref
                        and u.material_id == material.id
                        and u.version_no == version_no
                    ),
                    None,
                )
                if scope is None:
                    out.append(
                        (owner, material_id, version_no, "gone", None, False, None)
                    )
                    continue
                frozen_license = str(scope.get("license") or "self")
            via = license_rank("via_expert")
            shareable = (
                state == "live"
                and license_rank(license_) >= via
                and (frozen_license is None or license_rank(frozen_license) >= via)
            )
            out.append(
                (
                    owner,
                    material_id,
                    version_no,
                    state,
                    license_,
                    shareable,
                    frozen_license,
                )
            )
        return out

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
        if len(items) > 200:
            raise refused("items_invalid", "give at most 200 items")
        if not items:
            # 没有要看的资料也记一条（账号级）：这次过程本人同意过，撤了就没了；
            # 只带公开原件、引用的平台重跑凭它签机器授权
            return [
                await self.repo.add_use(
                    Use(
                        id=self.new_id(),
                        subject=subject,
                        use=use,
                        created_at=self.now(),
                        created_via=f"user:{app}",
                        scope={"kind": kind, "ref": ref},
                    )
                )
            ]
        made: list[Use] = []
        for material_id, version_no in dict.fromkeys(items):
            material, holding = await self._held_version(
                subject, material_id, version_no
            )
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

    async def live_subscription(
        self, caller: str, subject: str, subscription_id: str, revision: int
    ) -> Use:
        """knowfeed 生成新需求前核：这一修订的订阅范围同意还在不在。

        本人可以在 knowbase web端撤（0044 第 2.6 节），knowfeed 后台跑订阅时没有本人的
        令牌，只能凭服务身份来问；撤了回 404，knowfeed 就不再生成新需求。
        """
        if caller != "knowfeed":
            raise denied_scope()
        use = subscription_use(subscription_id, revision)
        for found in await self.repo.named(subject, use):
            if found.use == use:
                return found
        raise not_found("subscription")

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

    async def freeze_binding(
        self, author: str, expert_id: str, version: str, *, preview: bool = False
    ) -> list[tuple[Use, str]]:
        """交付时把「绑给专家」冻结成 `expert:<专家>@<版本>`，每份钉在当时那一版。

        knowlab 交付那一刻调；以后作者取消绑定、传新版，都只影响下一版。冻结只一次：
        同一版再调回的是第一次冻结的那几条（版本、许可都照第一次，摘要按冻结的那一版
        取），不会照现在的版本、许可另冻一份——买家请这一版时读的就是冻结的那一版。
        这一版冻结时还没有、后来才绑的那几份，同一版里也不再冻进来（要出新一版）。
        preview：不写，回「现在冻结会冻成什么」，给 knowlab 提交前预查许可用。
        每条连同钉住那一版的摘要回去（knowlab 照它写清单 v2 的 `bindings`）。
        """
        frozen = frozen_expert_use(expert_id, version)
        if expert_id not in {e.expert_id for e in await self.repo.experts_of(author)}:
            raise not_found("expert")
        already = await self.repo.named(author, frozen)
        already = [u for u in already if u.use == frozen]
        made: list[tuple[Use, str]] = []
        if already:
            for use in sorted(already, key=lambda u: str(u.material_id)):
                assert use.material_id is not None and use.version_no is not None
                pinned = await self.repo.version(use.material_id, use.version_no)
                if pinned is None:
                    raise not_found("version")
                made.append((use, pinned.sha256))
            return made
        bound = await self.repo.material_ids_with_use(author, f"expert:{expert_id}")
        for material_id in sorted(bound):
            material, holding = await self._held_version(author, material_id, None)
            version_no = holding.version_no or material.current_version
            pinned = await self.repo.version(material.id, version_no)
            if pinned is None:
                raise not_found("version")
            # 交付那一刻的许可：给别人用要「可以经专家给别人用」以上，作者后来
            # 改低只影响新的销售（0043 第 2.2 节）。自己的取资料现在的许可档，
            # 买来的取成交时定的
            license_ = (
                material.license if holding.version_no is None else holding.license
            )
            use = Use(
                id=self.new_id(),
                subject=author,
                use=frozen,
                created_at=self.now(),
                created_via="knowlab-delivery",
                material_id=material.id,
                version_no=version_no,
                scope={"license": license_},
            )
            if not preview:
                use = await self.repo.add_use(use)
            made.append((use, pinned.sha256))
        return made

    async def raise_frozen_license(
        self,
        caller: str,
        author: str,
        expert_id: str,
        version: str,
        material_id: str,
        license_: str,
        *,
        attest: bool,
    ) -> Use:
        """自用转公开时作者把冻结的那一份许可往高改（决定 10-30）：只许往高、要本人勾
        「我有权」、作者现在对这一份的许可确实不低于目标档；留许可留痕。清单不变，不用
        出新一版。knowlab 凭本人在场的那次提交代办。"""
        if caller != "knowlab":
            raise denied_scope()
        target = check_license(license_)
        if not attest:
            raise refused("attest_required", "raising the license needs an attestation")
        frozen = frozen_expert_use(expert_id, version)
        found = next(
            (
                u
                for u in await self.repo.named(author, frozen)
                if u.use == frozen and u.material_id == material_id
            ),
            None,
        )
        if found is None or found.version_no is None:
            raise not_found("binding")
        old = str(found.scope.get("license") or "self")
        if license_rank(target) <= license_rank(old):
            raise refused("license_not_higher", "the frozen license only goes up")
        material, holding = await self._held_version(
            author, material_id, found.version_no
        )
        current = material.license if holding.version_no is None else holding.license
        if not material.live or license_rank(current) < license_rank(target):
            raise refused(
                "license_too_low", "the current license is lower than the target"
            )
        raised = await self.repo.raise_frozen_license(
            author, frozen, material.id, found.version_no, target, self.now()
        )
        if raised is None:
            raise not_found("binding")
        return raised

    # —— 公开原件：knowfeed 交来，只存一份（0047 第二节） ——

    async def deliver_public(
        self,
        caller: str,
        channel: str,
        channel_doc_id: str,
        name: str,
        content: bytes,
        description: Description,
        *,
        source_url: str | None = None,
        references: list[tuple[str, str]] | None = None,
    ) -> tuple[Material, ContentVersion, list[Holding]]:
        """knowfeed 交一份公开披露：同一渠道同一编号只存一份，内容变了记新版本；
        给提了需求的用户各记一条可用的引用（取得依据「引用」，ref 记需求依据编号）。

        描述按入库质检过算（参与解析）；许可由系统定为「可以经专家给别人用」。
        """
        if caller not in ("knowfeed",):
            raise denied_scope()
        if not _CHANNEL.fullmatch(channel) or not _CHANNEL_DOC.fullmatch(
            channel_doc_id
        ):
            raise refused("channel_invalid", "channel or document id is invalid")
        if source_url is not None and not source_url.startswith(
            ("https://", "http://")
        ):
            raise refused("url_invalid", "source url is invalid")
        filename = clean_name(name)
        media_type = kind_of(filename, content)
        if not 0 < len(content) <= MAX_PUBLIC_BYTES:
            raise UserDataError(413, "too_large", "public originals are at most 200 MB")
        checked = replace(
            description,
            data_format=description.data_format or format_of_media(media_type),
            status="checked",
            vocab_version=VOCAB_VERSION,
        ).checked()
        sha = hashlib.sha256(content).hexdigest()
        material = await self.repo.find_public(channel, channel_doc_id)
        now = self.now()
        if material is None:
            material_id = self.new_id()
            version = await self._store(
                PUBLIC_OWNER, material_id, 1, media_type, content, now
            )
            material = await self.repo.add_material(
                Material(
                    id=material_id,
                    owner=PUBLIC_OWNER,
                    name=filename,
                    content_origin="disclosure",
                    license="via_expert",
                    description=checked,
                    current_version=1,
                    created_at=now,
                    provenance={
                        "channel": channel,
                        "channel_doc_id": channel_doc_id,
                        "source_url": source_url,
                    },
                ),
                version,
                Holding(
                    id=self.new_id(),
                    subject=PUBLIC_OWNER,
                    material_id=material_id,
                    basis="collect",
                    license="via_expert",
                    created_at=now,
                ),
            )
        else:
            latest = await self.repo.version(material.id, material.current_version)
            assert latest is not None
            if latest.sha256 == sha:
                version = latest
            else:
                # 更正版：同一份资料的新内容版本，旧版照旧留着
                version = await self._store(
                    PUBLIC_OWNER,
                    material.id,
                    material.current_version + 1,
                    media_type,
                    content,
                    now,
                )
                material = await self.repo.add_version(version)
        made = await self.add_public_references(
            caller, material.id, version.version_no, references or []
        )
        return material, version, made

    async def add_public_references(
        self,
        caller: str,
        material_id: str,
        version_no: int,
        references: list[tuple[str, str]],
    ) -> list[Holding]:
        """给提了需求的用户各记一条可用的引用，到交来的那一版；同一需求依据只记一条。"""
        if caller not in ("knowfeed",):
            raise denied_scope()
        material = await self.repo.get_material(material_id)
        if material is None or material.owner != PUBLIC_OWNER:
            raise not_found()
        if await self.repo.version(material.id, version_no) is None:
            raise not_found("version")
        made: list[Holding] = []
        for subject, ref in dict.fromkeys(references):
            if not subject or subject == PUBLIC_OWNER or not ref or len(ref) > 255:
                raise refused("reference_invalid", "reference needs a user and a ref")
            made.append(
                await self.repo.add_holding(
                    Holding(
                        id=self.new_id(),
                        subject=subject,
                        material_id=material.id,
                        basis="reference",
                        license="via_expert",
                        created_at=self.now(),
                        version_no=version_no,
                        ref=ref,
                    )
                )
            )
        return made

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

    # —— 管理端（0044 第四节）：用户资料用量、词表、禁用资料 ——

    async def usage_overview(self, limit: int = 100) -> list[dict[str, object]]:
        return await self.repo.usage_by_subject(max(1, min(limit, 500)))

    async def description_overview(self) -> list[dict[str, object]]:
        return await self.repo.description_stats()

    async def find_for_admin(
        self,
        *,
        owner: str | None = None,
        material_id: str | None = None,
        disabled: bool = False,
    ) -> list[Material]:
        if owner is None and material_id is None and not disabled:
            raise refused("filter_required", "give an owner, an id, or disabled")
        return await self.repo.find_materials(
            owner=owner, material_id=material_id, disabled=disabled
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

    async def _held_version(
        self, subject: str, material_id: str, version_no: int | None
    ) -> tuple[Material, Holding]:
        """同 `_held`，但按版本挑使用权：所有者的那条（不限版本）优先；给了版本就挑覆盖
        这一版的；没给就挑版本最新的那条（多次买、多版本引用时 `holdings[0]` 可能不覆盖
        要的那一版）。"""
        material = await self.repo.get_material(material_id)
        if material is None or material.disabled_at is not None:
            raise not_found()
        holdings = await self.repo.holdings_of(subject, material.id)
        owner = next((h for h in holdings if h.version_no is None), None)
        if owner is not None:
            return material, owner
        if not holdings:
            raise not_found()
        if version_no is not None:
            found = next((h for h in holdings if h.covers(version_no)), None)
            if found is None:  # 有使用权、只是不到这一版
                raise not_found("version")
            return material, found
        return material, max(holdings, key=lambda h: h.version_no or 0)

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
