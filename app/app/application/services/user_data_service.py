"""用户资料：上传、本人读删、同意依据、运行授权、凭授权读、买来复制（SDD 0042）。

谁能做什么：
- 用户本人（换来的令牌，`sub` + `azp`）：只动自己在这个应用分区里的资料；
  签委托、会话、孵化三种运行授权。
- knowlab（服务身份）：凭用户本人留下的同意依据签测试运行授权，范围只能更小。
- 读的一方（服务身份，现在是 knowbench 的工作台）：
  凭一条有效的运行授权读其中钉住的那几份。
- knowmart（服务身份）：成交时把卖家 knowmart 知识库里的一份复制进买家选的知识库。
每次读内容、读格式、复制都留痕。
"""

from __future__ import annotations

import hashlib
import io
import re
import uuid
import zipfile
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime

from app.application.ports.user_data import UserDataRepository, UserObjects
from app.domain.user_data import (
    APPS,
    GRANT_SCOPE,
    MAX_CONSENT_ITEMS,
    RECEIVING_APPS,
    USER_GRANT_KINDS,
    App,
    Consent,
    Grant,
    GrantKind,
    Item,
    Owner,
    Pin,
    ReadEvent,
    Scope,
    UserDataError,
    check_pins,
    check_purpose,
    check_ref,
    check_size,
    clean_name,
    conflict,
    grant_expiry,
    kind_of,
    not_found,
    object_key,
    pins_match,
    refused,
)

# 各种运行授权读的是哪个应用分区（0042 第三节）
GRANT_PARTITION: dict[GrantKind, App] = {
    "test": "knowlab",
    "delegation": "knowbench",
    "session": "knowbench",
    "incubation": "knowlab",
}
TEST_PURPOSE = "knowlab-test"


@dataclass(frozen=True)
class FormatView:
    """只看格式：表格有哪几张表、头一行是什么；不给内容（孵化授权读的就是这个）。"""

    sheets: tuple[str, ...]
    header: str | None


class UserDataService:
    def __init__(
        self,
        repo: UserDataRepository,
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

    # —— 本人 ——

    async def upload(
        self,
        owner: Owner,
        name: str | None,
        content: bytes,
        origin_ref: str | None = None,
    ) -> Item:
        filename = clean_name(name)
        media_type = kind_of(filename, content)
        check_size(len(content), await self.repo.bytes_used(owner))
        item_id = self.new_id()
        key = object_key(self.prefix, owner, item_id)
        # 先存对象再记：记没写上，只多一个没人引用的对象，不会有指向空处的记录
        await self.objects.put(key, content, media_type)
        return await self.repo.add_item(
            Item(
                id=item_id,
                owner=owner,
                name=filename,
                media_type=media_type,
                size_bytes=len(content),
                sha256=hashlib.sha256(content).hexdigest(),
                origin="upload",
                origin_ref=origin_ref,
                object_key=key,
                created_at=self.now(),
            )
        )

    async def import_item(
        self, owner: Owner, name: str | None, content: bytes, origin_ref: str
    ) -> Item:
        """应用把自己原来存的资料搬进自己的分区（knowlab 知识库搬家，SDD 0042 第七节）。

        按调用方给的原编号幂等：同一个编号、同样的内容回已搬的那份；内容不同是 409。
        记成本人上传的（origin 照旧是 upload，origin_ref 写原编号）。
        """
        check_ref(origin_ref, "origin_ref")
        existing = await self.repo.find_by_origin(owner, origin_ref)
        if existing is not None and existing.deleted_at is None:
            if existing.sha256 != hashlib.sha256(content).hexdigest():
                raise conflict(
                    "origin_ref_reused", "this ref was imported with other content"
                )
            return existing
        return await self.upload(owner, name, content, origin_ref=origin_ref)

    async def items_of(self, owner: Owner) -> list[Item]:
        return await self.repo.list_items(owner)

    async def get(self, owner: Owner, item_id: str) -> Item:
        item = await self.repo.get_item(item_id)
        if item is None or item.owner != owner or item.deleted_at is not None:
            raise not_found()
        return item

    async def download(self, owner: Owner, item_id: str) -> tuple[Item, bytes]:
        item = await self.get(owner, item_id)
        content = await self._content(item)
        await self._log(item, reader=owner.subject, operation="owner-download")
        return item, content

    async def delete(self, owner: Owner, item_id: str) -> None:
        item = await self.get(owner, item_id)
        await self.repo.delete_item(item.id, self.now())
        await self.objects.delete(item.object_key)

    async def consent(
        self, owner: Owner, purpose: str, ref: str, pins: tuple[Pin, ...]
    ) -> Consent:
        check_purpose(purpose)
        check_ref(ref)
        await self._own_pins(owner, check_pins(pins, MAX_CONSENT_ITEMS))
        return await self.repo.add_consent(
            Consent(
                id=self.new_id(),
                owner=owner,
                purpose=purpose,
                ref=ref,
                pins=pins,
                created_at=self.now(),
            )
        )

    async def withdraw_consent(self, owner: Owner, consent_id: str) -> None:
        consent = await self.repo.get_consent(consent_id)
        if consent is None or consent.owner != owner:
            raise not_found("consent")
        if consent.withdrawn_at is None:
            await self.repo.withdraw_consent(consent_id, self.now())

    async def grant_by_user(
        self,
        owner: Owner,
        kind: GrantKind,
        reader_app: App,
        ref: str,
        pins: tuple[Pin, ...],
        ttl_seconds: int | None = None,
    ) -> Grant:
        if kind not in USER_GRANT_KINDS:
            raise refused("kind_invalid", "this kind of grant is not issued by users")
        if owner.app != GRANT_PARTITION[kind]:
            raise refused(
                "partition_invalid",
                f"a {kind} grant reads the {GRANT_PARTITION[kind]} knowledge base",
            )
        if reader_app not in APPS:
            raise refused("reader_invalid", "reader_app is invalid")
        check_ref(ref)
        await self._own_pins(owner, check_pins(pins))
        return await self._issue(
            kind, owner, reader_app, ref, pins, owner.subject, ttl_seconds, None
        )

    async def revoke_by_user(self, owner: Owner, grant_id: str) -> None:
        grant = await self.repo.get_grant(grant_id)
        if grant is None or grant.owner != owner:
            raise not_found("grant")
        await self._revoke(grant)

    # —— knowlab：凭同意依据签测试授权 ——

    async def grant_test(
        self,
        issuer: str,
        owner_subject: str,
        consent_id: str,
        reader_app: App,
        ref: str,
        pins: tuple[Pin, ...],
        ttl_seconds: int | None = None,
    ) -> Grant:
        owner = Owner(owner_subject, "knowlab")
        consent = await self.repo.get_consent(consent_id)
        if (
            consent is None
            or consent.owner != owner
            or consent.purpose != TEST_PURPOSE
            or consent.withdrawn_at is not None
        ):
            raise not_found("consent")
        if reader_app not in APPS:
            raise refused("reader_invalid", "reader_app is invalid")
        check_ref(ref)
        check_pins(pins)
        allowed = set(consent.pins)
        if any(p not in allowed for p in pins):
            # 范围只能比同意依据小（数据拓扑第 5.3 节第 2 条）
            raise UserDataError(
                403, "outside_consent", "an item is outside the consent"
            )
        await self._own_pins(owner, pins)
        return await self._issue(
            "test", owner, reader_app, ref, pins, issuer, ttl_seconds, consent.id
        )

    async def revoke_by_service(self, issuer: str, grant_id: str) -> None:
        grant = await self.repo.get_grant(grant_id)
        if grant is None or grant.issued_by != issuer:
            raise not_found("grant")
        await self._revoke(grant)

    # —— 读的一方：凭授权读 ——

    async def read(
        self, reader_app: App, reader: str, grant_id: str, item_id: str, scope: Scope
    ) -> tuple[Item, bytes | FormatView]:
        grant, item = await self._granted(reader_app, grant_id, item_id, scope)
        content = await self._content(item)
        await self._log(item, reader=reader, operation=scope, grant_id=grant.id)
        if scope == "format":
            return item, describe_format(item, content)
        return item, content

    async def check(
        self, reader_app: App, reader: str, grant_id: str, item_id: str
    ) -> Item:
        """读的一方用自己缓存的内容之前，核这次读还算不算数（不取内容）。

        与 read 同样的判断：授权在、给这个应用、没撤没过期、钉了这一份、资料没删、
        版本对得上；同样留一条读数记录（operation=check）。读的一方不能让缓存跳过授权。
        """
        grant, item = await self._granted(reader_app, grant_id, item_id, None)
        await self._log(item, reader=reader, operation="check", grant_id=grant.id)
        return item

    async def _granted(
        self, reader_app: App, grant_id: str, item_id: str, scope: Scope | None
    ) -> tuple[Grant, Item]:
        grant = await self.repo.get_grant(grant_id)
        # 授权不在、不是给这个应用的，与没有一样；过期、撤了是确定的拒绝
        if grant is None or grant.reader_app != reader_app:
            raise not_found("grant")
        if not grant.active(self.now()):
            raise UserDataError(
                403, "grant_inactive", "the grant is revoked or expired"
            )
        pin = grant.pin(item_id)
        if pin is None:
            raise not_found()
        if scope == "content" and grant.scope != "content":
            raise UserDataError(403, "format_only", "this grant reads formats only")
        item = await self.repo.get_item(item_id)
        if item is None or item.owner != grant.owner or item.deleted_at is not None:
            raise not_found()
        if item.sha256 != pin.sha256:
            raise conflict("sha256_mismatch", "the pinned version does not match")
        return grant, item

    async def release(self, reader_app: App, grant_id: str) -> None:
        """读的一方交还给自己的授权（委托、会话结束时；只会收窄，不用本人在场）。

        已撤、已过期的再交还一次照样成功。
        """
        grant = await self.repo.get_grant(grant_id)
        if grant is None or grant.reader_app != reader_app:
            raise not_found("grant")
        await self._revoke(grant)

    async def grant_view(self, reader_app: App, grant_id: str) -> Grant:
        """读的一方看这条授权钉了哪几份（列清单用）。"""
        grant = await self.repo.get_grant(grant_id)
        if grant is None or grant.reader_app != reader_app:
            raise not_found("grant")
        return grant

    # —— knowmart：成交时复制 ——

    async def purchase(
        self,
        reader: str,
        seller_subject: str,
        seller_item_id: str,
        buyer_subject: str,
        buyer_app: App,
        transaction_ref: str,
    ) -> Item:
        if buyer_app not in RECEIVING_APPS:
            raise refused(
                "buyer_app_invalid", "bought knowledge goes to knowbench or knowlab"
            )
        check_ref(transaction_ref, "transaction_ref")
        buyer = Owner(buyer_subject, buyer_app)
        existing = await self.repo.find_by_origin(buyer, transaction_ref)
        if existing is not None:
            return existing  # 同一笔成交重发，回已复制的那份
        source = await self.repo.get_item(seller_item_id)
        if (
            source is None
            or source.owner != Owner(seller_subject, "knowmart")
            or source.deleted_at is not None
        ):
            raise not_found()
        check_size(source.size_bytes, await self.repo.bytes_used(buyer))
        item_id = self.new_id()
        key = object_key(self.prefix, buyer, item_id)
        await self.objects.copy(source.object_key, key)
        copied = await self.repo.add_item(
            Item(
                id=item_id,
                owner=buyer,
                name=source.name,
                media_type=source.media_type,
                size_bytes=source.size_bytes,
                sha256=source.sha256,
                origin="purchase",
                origin_ref=transaction_ref,
                object_key=key,
                created_at=self.now(),
            )
        )
        await self._log(source, reader=reader, operation="purchase-copy")
        return copied

    # —— 内部 ——

    async def _own_pins(self, owner: Owner, pins: tuple[Pin, ...]) -> None:
        items = await self.repo.items([p.item_id for p in pins])
        # 不是这个分区的当没有
        mine = {k: v for k, v in items.items() if v.owner == owner}
        pins_match(pins, mine)

    async def _issue(
        self,
        kind: GrantKind,
        owner: Owner,
        reader_app: App,
        ref: str,
        pins: tuple[Pin, ...],
        issued_by: str,
        ttl_seconds: int | None,
        consent_id: str | None,
    ) -> Grant:
        now = self.now()
        return await self.repo.add_grant(
            Grant(
                id=self.new_id(),
                kind=kind,
                owner=owner,
                reader_app=reader_app,
                ref=ref,
                pins=pins,
                scope=GRANT_SCOPE[kind],
                issued_by=issued_by,
                issued_at=now,
                expires_at=grant_expiry(kind, now, ttl_seconds),
                consent_id=consent_id,
            )
        )

    async def _revoke(self, grant: Grant) -> None:
        if grant.revoked_at is None:
            await self.repo.revoke_grant(grant.id, self.now())

    async def _content(self, item: Item) -> bytes:
        content = await self.objects.get(item.object_key)
        if hashlib.sha256(content).hexdigest() != item.sha256:
            # 存储里的东西和记录对不上：不给，不当成别的版本
            raise UserDataError(
                503, "storage_mismatch", "stored content does not match"
            )
        return content

    async def _log(
        self,
        item: Item,
        *,
        reader: str,
        operation: str,
        grant_id: str | None = None,
    ) -> None:
        await self.repo.record_read(
            ReadEvent(
                owner=item.owner,
                item_id=item.id,
                sha256=item.sha256,
                reader=reader,
                operation=operation,  # type: ignore[arg-type]
                grant_id=grant_id,
                at=self.now(),
            )
        )


_SHEET = re.compile(rb'<sheet\b[^>]*\bname="([^"]{1,100})"')


def describe_format(item: Item, content: bytes) -> FormatView:
    """表格给表名（xlsx）或头一行（csv、txt、md）。

    PDF、xls 只给类型与大小（在 Item 上）。
    """
    if item.media_type.startswith(
        "application/vnd.openxmlformats-officedocument.spreadsheetml"
    ):
        try:
            with zipfile.ZipFile(io.BytesIO(content)) as book:
                workbook = book.read("xl/workbook.xml")
        except (zipfile.BadZipFile, KeyError):
            return FormatView(sheets=(), header=None)
        names = tuple(m.decode("utf-8", "replace") for m in _SHEET.findall(workbook))
        return FormatView(sheets=names[:50], header=None)
    if item.media_type.startswith("text/"):
        first = content.split(b"\n", 1)[0][:500].decode("utf-8", "replace").strip()
        return FormatView(sheets=(), header=first or None)
    return FormatView(sheets=(), header=None)
