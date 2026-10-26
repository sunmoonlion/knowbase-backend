"""资料库（数据第四版，SDD 0043 块 1a）：真数据库，含迁移 0010 的回填。"""

from __future__ import annotations

import importlib.util
import os
import uuid
from datetime import UTC, date, datetime, timedelta

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import create_async_engine
from test_durable_delivery_db import ROOT, sql
from test_durable_delivery_db import db as db
from test_user_data import CSV, Clock, MemoryObjects

from app.application.services.user_data_service import UserDataService
from app.application.services.user_library_service import UserLibraryService
from app.domain.user_data import Owner, UserDataError
from app.domain.user_library import Description
from app.infrastructure.repositories.user_data import SqlUserDataRepository
from app.infrastructure.repositories.user_library import SqlUserLibraryRepository
from app.infrastructure.storage.postgres import make_session_factory

ALICE = "alice"
BOB = "bob"
PDF = b"%PDF-1.7\n" + b"x" * 100


def library(db, objects=None, clock=None) -> UserLibraryService:
    return UserLibraryService(
        SqlUserLibraryRepository(lambda: db),
        objects or MemoryObjects(),
        prefix="user-data",
        clock=clock or Clock(),
    )


async def refused(work, status: int, code: str) -> None:
    with pytest.raises(UserDataError) as caught:
        await work
    assert (caught.value.status, caught.value.code) == (status, code)


async def test_upload_records_material_first_version_and_owner_holding(db):
    lib = library(db)
    material, version = await lib.upload(
        ALICE,
        "maotai-2024.pdf",
        PDF,
        description=Description(
            subject="600519.SH",
            doc_type="annual_report",
            period_start=date(2024, 1, 1),
            period_end=date(2024, 12, 31),
            disclosed_on=date(2025, 4, 2),
        ),
        confirm=True,
    )
    assert material.owner == ALICE and material.legacy_app is None
    assert material.content_origin == "upload" and material.license == "self"
    assert material.description.status == "confirmed"
    assert material.description.data_format == "pdf"
    assert material.description.resolvable
    assert version.version_no == 1 and material.current_version == 1
    shelf = await lib.shelf(ALICE)
    assert [(e.material.id, e.category, e.version.version_no) for e in shelf] == [
        (material.id, "uploaded", 1)
    ]
    # v1 的缓存栏是当前版本，app 为空（不按应用分区）
    assert (
        await sql(
            db,
            "SELECT app FROM user_data_item WHERE id = :id",
            id=uuid.UUID(material.id),
        )
        is None
    )
    assert await lib.shelf(BOB) == []


async def test_unconfirmed_or_unknown_descriptions(db):
    lib = library(db)
    material, _ = await lib.upload(
        ALICE, "notes.csv", CSV, description=Description(subject="600519.SH")
    )
    assert material.description.status == "unconfirmed"
    assert not material.description.resolvable
    await refused(
        lib.upload(ALICE, "x.csv", CSV, description=Description(subject="maotai")),
        422,
        "subject_invalid",
    )
    await refused(
        lib.upload(ALICE, "x.csv", CSV, description=Description(doc_type="novel")),
        422,
        "doc_type_invalid",
    )
    await refused(
        lib.upload(
            ALICE,
            "x.csv",
            CSV,
            description=Description(
                period_start=date(2025, 1, 1), period_end=date(2024, 1, 1)
            ),
        ),
        422,
        "period_invalid",
    )


async def test_new_versions_keep_old_ones_and_move_the_cache(db):
    objects = MemoryObjects()
    lib = library(db, objects)
    material, first = await lib.upload(ALICE, "table.csv", CSV)
    updated, second = await lib.add_version(ALICE, material.id, None, CSV + b"3,4\n")
    assert (second.version_no, updated.current_version) == (2, 2)
    assert [v.version_no for v in await lib.versions(ALICE, material.id)] == [1, 2]
    old, content = await lib.download(ALICE, "knowbench", material.id, 1)
    assert (old.sha256, content) == (first.sha256, CSV)
    current, content = await lib.download(ALICE, "knowbench", material.id)
    assert current.version_no == 2 and content == CSV + b"3,4\n"
    assert (
        await sql(
            db,
            "SELECT sha256 FROM user_data_item WHERE id = :id",
            id=uuid.UUID(material.id),
        )
        == second.sha256
    )
    # 读数留痕：只记编号，app 记代办的应用
    assert (
        await sql(
            db,
            "SELECT count(*) FROM user_data_read WHERE item_id = :id AND app = "
            "'knowbench' AND operation = 'owner-download'",
            id=uuid.UUID(material.id),
        )
        == 2
    )
    # 别人不能传新版本
    await refused(lib.add_version(BOB, material.id, None, CSV), 404, "item_not_found")


async def test_describe_logs_and_does_not_make_a_version(db):
    lib = library(db)
    material, _ = await lib.upload(ALICE, "table.csv", CSV)
    described = await lib.describe(
        ALICE,
        material.id,
        Description(subject="000001.SZ", doc_type="statistics"),
        confirm=True,
    )
    assert described.description.subject == "000001.SZ"
    assert described.description.status == "confirmed"
    assert described.current_version == 1
    history = await lib.history(ALICE, material.id)
    assert [h["what"] for h in history] == ["description"]
    assert history[0]["after"]["subject"] == "000001.SZ"
    await refused(
        lib.describe(BOB, material.id, Description(), confirm=False),
        404,
        "item_not_found",
    )


async def test_license_rules(db):
    lib = library(db)
    material, _ = await lib.upload(ALICE, "table.csv", CSV)
    await refused(
        lib.set_license(ALICE, material.id, "sale", attest=False),
        422,
        "attest_required",
    )
    raised = await lib.set_license(ALICE, material.id, "sale", attest=True)
    assert raised.license == "sale"
    lowered = await lib.set_license(ALICE, material.id, "self", attest=False)
    assert lowered.license == "self"
    history = await lib.history(ALICE, material.id)
    assert [h["after"] for h in history] == [
        {"license": "sale", "attested": True},
        {"license": "self", "attested": False},
    ]
    # 采集来的许可由系统定
    await sql(
        db,
        "UPDATE user_data_item SET content_origin = 'collected' WHERE id = :id",
        id=uuid.UUID(material.id),
    )
    await refused(
        lib.set_license(ALICE, material.id, "via_expert", attest=True),
        422,
        "license_fixed",
    )


async def test_remove_is_logical_and_keeps_buyers(db):
    objects = MemoryObjects()
    lib = library(db, objects)
    material, version = await lib.upload(ALICE, "table.csv", CSV)
    # 一条买家的使用权（块 8 由 knowmart 建；这里直接写），只到第 1 版
    await sql(
        db,
        "INSERT INTO user_data_holding (id, subject, item_id, basis, license, "
        "version_no, ref, created_at) VALUES (:id, :bob, :item, 'purchase', 'self', "
        "1, 'tx-1', now())",
        id=uuid.uuid4(),
        bob=BOB,
        item=uuid.UUID(material.id),
    )
    await lib.add_version(ALICE, material.id, None, CSV + b"9,9\n")
    assert [v.version_no for v in await lib.versions(BOB, material.id)] == [1]
    await refused(lib.download(BOB, None, material.id, 2), 404, "version_not_found")
    await lib.remove(ALICE, material.id)
    assert await lib.shelf(ALICE) == []
    with pytest.raises(UserDataError):
        await lib.get(ALICE, material.id)
    # 买家照旧看得到、读得到交易那一版；内容没删
    entry = await lib.get(BOB, material.id)
    assert (entry.category, entry.version.version_no) == ("purchased", 1)
    _, content = await lib.download(BOB, "knowlab", material.id)
    assert content == CSV and version.object_key in objects.data


async def test_space_counts_all_versions_and_what_buyers_still_hold(db):
    lib = library(db)
    material, _ = await lib.upload(ALICE, "table.csv", CSV)
    await lib.add_version(ALICE, material.id, None, CSV + b"1\n")
    used = await lib.repo.bytes_used(ALICE)
    assert used == len(CSV) * 2 + 2
    other, _ = await lib.upload(ALICE, "other.csv", CSV)
    await lib.remove(ALICE, other.id)
    assert await lib.repo.bytes_used(ALICE) == used
    await sql(
        db,
        "INSERT INTO user_data_holding (id, subject, item_id, basis, license, "
        "version_no, ref, created_at) VALUES (:id, :bob, :item, 'purchase', 'self', "
        "1, 'tx-2', now())",
        id=uuid.uuid4(),
        bob=BOB,
        item=uuid.UUID(material.id),
    )
    await lib.remove(ALICE, material.id)
    assert await lib.repo.bytes_used(ALICE) == used  # 买家还在，照算


async def test_v1_uploads_show_up_in_the_library(db):
    v1 = UserDataService(SqlUserDataRepository(lambda: db), MemoryObjects(), prefix="p")
    item = await v1.upload(Owner(ALICE, "knowlab"), "old.csv", CSV)
    lib = library(db)
    entry = await lib.get(ALICE, item.id)
    assert entry.material.legacy_app == "knowlab"
    assert (entry.category, entry.version.sha256) == ("uploaded", item.sha256)
    await v1.delete(Owner(ALICE, "knowlab"), item.id)
    assert await lib.shelf(ALICE) == []


async def test_migration_backfills_old_rows():
    url = os.environ.get("DELIVERY_TEST_DATABASE_URL")
    if not url:
        pytest.skip("set DELIVERY_TEST_DATABASE_URL to a disposable *_tests database")
    parsed = make_url(url)
    assert parsed.database and parsed.database.endswith("_tests")
    schema = "library_test_" + uuid.uuid4().hex
    engine = create_async_engine(
        parsed.set(drivername="postgresql+asyncpg"),
        connect_args={"server_settings": {"search_path": schema + ",public"}},
    )
    paths = sorted((ROOT / "alembic/versions").glob("20*.py"))
    before = [p for p in paths if p.name < "20261026_0010"]
    target = [p for p in paths if p.name.startswith("20261026_0010")]
    assert len(target) == 1

    def run(connection, chosen):
        with Operations.context(MigrationContext.configure(connection)):
            for path in chosen:
                spec = importlib.util.spec_from_file_location(path.stem, path)
                module = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(module)
                module.upgrade()

    now = datetime(2026, 10, 23, 12, 0, tzinfo=UTC)
    rows = {
        "kept": (uuid.uuid4(), "upload", None, None),
        "deleted": (uuid.uuid4(), "upload", None, now + timedelta(hours=1)),
        "bought": (uuid.uuid4(), "purchase", "tx-9", None),
    }
    try:
        async with engine.begin() as c:
            await c.execute(text(f'CREATE SCHEMA "{schema}"'))
            await c.execute(
                text('CREATE EXTENSION IF NOT EXISTS "uuid-ossp" WITH SCHEMA public')
            )
            await c.run_sync(run, before)
            for key, (item_id, origin, ref, deleted) in rows.items():
                await c.execute(
                    text(
                        "INSERT INTO user_data_item (id, owner_subject, app, name, "
                        "media_type, size_bytes, sha256, origin, origin_ref, "
                        "object_key, created_at, deleted_at, no_resale) VALUES "
                        "(:id, 'alice', 'knowbench', :name, 'text/csv; charset=utf-8',"
                        " 10, :sha, :origin, :ref, :key, :at, :deleted, false)"
                    ),
                    {
                        "id": item_id,
                        "name": key + ".csv",
                        "sha": "b" * 64,
                        "origin": origin,
                        "ref": ref,
                        "key": "k/" + key,
                        "at": now,
                        "deleted": deleted,
                    },
                )
            await c.run_sync(run, target)
        db = make_session_factory(engine)
        versions = await sql(db, "SELECT count(*) FROM user_data_version")
        assert versions == 3
        assert (
            await sql(
                db,
                "SELECT object_key FROM user_data_version WHERE item_id = :id AND "
                "version_no = 1",
                id=rows["kept"][0],
            )
            == "k/kept"
        )
        # 已删的：所有者的使用权已结束、不进总栏；买来的：只到第 1 版、带交易编号
        assert (
            await sql(
                db,
                "SELECT end_reason FROM user_data_holding WHERE item_id = :id",
                id=rows["deleted"][0],
            )
            == "deleted"
        )
        assert (
            await sql(
                db,
                "SELECT version_no FROM user_data_holding WHERE item_id = :id AND "
                "ref = 'tx-9'",
                id=rows["bought"][0],
            )
            == 1
        )
        shelf = await SqlUserLibraryRepository(lambda: db).shelf("alice")
        assert sorted(e.material.name for e in shelf) == ["bought.csv", "kept.csv"]
        assert all(e.material.legacy_app == "knowbench" for e in shelf)
    finally:
        async with engine.begin() as c:
            await c.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
        await engine.dispose()
