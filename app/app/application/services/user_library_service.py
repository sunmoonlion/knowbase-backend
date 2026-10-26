"""资料库：上传、内容版本、描述、许可档、总栏、下载、删（SDD 0043 第二节，块 1a）。

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
    kind_of,
    not_found,
    refused,
)
from app.domain.user_library import (
    CATEGORY_OF_BASIS,
    ContentVersion,
    Description,
    Holding,
    License,
    Material,
    ShelfEntry,
    check_license,
    license_rank,
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
    ) -> tuple[Material, ContentVersion]:
        """用户上传一份新资料：记资料、第 1 版、所有者的使用权（取得依据 upload）。

        描述是本人填的、勾了确认才算「用户确认过」（参与解析）；没填的格式按内容类型补。
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
        return saved, version

    async def add_version(
        self, subject: str, material_id: str, name: str | None, content: bytes
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

    # —— 内部 ——

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
