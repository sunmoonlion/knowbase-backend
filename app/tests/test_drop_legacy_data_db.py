"""迁移 20261028_0018（decisions ⑰）：旧数据集登记表与用户资料 v1 的同意依据、运行授权
四张表删掉；资料、读数留痕与第四版的表都留着。downgrade 建回的空表与 0017 时一模一样。
真 PostgreSQL、一次性 schema：先迁到 0017 造数，再单跑这一个迁移的 upgrade 与
downgrade。
"""

from __future__ import annotations

import importlib.util
import os
import uuid
from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import create_async_engine
from test_durable_delivery_db import ROOT

TARGET = ROOT / "alembic/versions/20261028_0018_drop_legacy_data.py"
BEFORE = [
    p
    for p in sorted((ROOT / "alembic/versions").glob("20*.py"))
    if p.name < TARGET.name
]
TABLES = (
    "knowledge_dataset",
    "user_data_consent",
    "user_data_consent_item",
    "user_data_grant",
    "user_data_grant_item",
)
NOW = datetime(2026, 10, 28, 12, 0, tzinfo=UTC)
SHA = "a" * 64


def _run(connection, paths, action: str = "upgrade") -> None:
    with Operations.context(MigrationContext.configure(connection)):
        for path in paths:
            spec = importlib.util.spec_from_file_location(path.stem, path)
            assert spec is not None and spec.loader is not None
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            getattr(module, action)()


@pytest_asyncio.fixture
async def at_0017():
    url = os.environ.get("DELIVERY_TEST_DATABASE_URL")
    if not url:
        pytest.skip("set DELIVERY_TEST_DATABASE_URL to a disposable *_tests database")
    parsed = make_url(url)
    assert parsed.database and parsed.database.endswith("_tests")
    schema = "drop_legacy_test_" + uuid.uuid4().hex
    engine = create_async_engine(
        parsed.set(drivername="postgresql+asyncpg"),
        connect_args={"server_settings": {"search_path": schema + ",public"}},
    )

    async def migrate(action: str) -> None:
        async with engine.begin() as c:
            await c.run_sync(_run, [TARGET], action)

    try:
        async with engine.begin() as c:
            await c.execute(text(f'CREATE SCHEMA "{schema}"'))
            await c.execute(
                text('CREATE EXTENSION IF NOT EXISTS "uuid-ossp" WITH SCHEMA public')
            )
            await c.run_sync(_run, BEFORE)
        yield engine, migrate
    finally:
        async with engine.begin() as c:
            await c.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
        await engine.dispose()


async def _all(engine, sql: str, **params) -> list[tuple]:
    async with engine.begin() as c:
        return [tuple(r) for r in (await c.execute(text(sql), params)).all()]


async def _shape(engine) -> dict[str, list[tuple]]:
    """五张表的列、约束、索引，用来比 downgrade 建回的与 0017 时的。"""
    names = {"names": list(TABLES)}
    return {
        "columns": await _all(
            engine,
            "SELECT table_name, column_name, data_type, character_maximum_length,"
            " is_nullable, column_default FROM information_schema.columns"
            " WHERE table_schema = current_schema() AND table_name = ANY(:names)"
            " ORDER BY table_name, column_name",
            **names,
        ),
        "constraints": await _all(
            engine,
            "SELECT t.relname, c.conname, pg_get_constraintdef(c.oid)"
            " FROM pg_constraint c JOIN pg_class t ON t.oid = c.conrelid"
            " WHERE t.relnamespace = current_schema()::regnamespace"
            " AND t.relname = ANY(:names) ORDER BY t.relname, c.conname",
            **names,
        ),
        "indexes": await _all(
            engine,
            "SELECT tablename, indexname, indexdef FROM pg_indexes"
            " WHERE schemaname = current_schema() AND tablename = ANY(:names)"
            " ORDER BY tablename, indexname",
            **names,
        ),
    }


async def _seed(engine) -> uuid.UUID:
    """0017 时的样子：一份 v1 时代的资料，它的同意依据、测试授权、一条读数留痕，
    外加一个登记过的数据集。返回资料的编号。"""
    item, consent, grant = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    async with engine.begin() as c:
        await c.execute(
            text(
                "INSERT INTO user_data_item (id, owner_subject, app, name, media_type,"
                " size_bytes, sha256, origin, object_key, created_at, no_resale)"
                " VALUES (:id, 'alice', 'knowlab', 'a.csv', 'text/csv', 10, :sha,"
                " 'upload', 'k/a', :at, false)"
            ),
            {"id": item, "sha": SHA, "at": NOW},
        )
        await c.execute(
            text(
                "INSERT INTO user_data_consent (id, owner_subject, app, purpose, ref)"
                " VALUES (:id, 'alice', 'knowlab', 'knowlab-test', 'knowbot:1')"
            ),
            {"id": consent},
        )
        await c.execute(
            text(
                "INSERT INTO user_data_consent_item (consent_id, item_id, sha256)"
                " VALUES (:c, :i, :sha)"
            ),
            {"c": consent, "i": item, "sha": SHA},
        )
        await c.execute(
            text(
                "INSERT INTO user_data_grant (id, kind, owner_subject, app, reader_app,"
                " ref, scope, issued_by, issued_at, expires_at, consent_id) VALUES"
                " (:id, 'test', 'alice', 'knowlab', 'knowbench', 'test:1', 'content',"
                " 'svc-lab', :at, :until, :c)"
            ),
            {"id": grant, "at": NOW, "until": NOW + timedelta(days=1), "c": consent},
        )
        await c.execute(
            text(
                "INSERT INTO user_data_grant_item (grant_id, item_id, sha256)"
                " VALUES (:g, :i, :sha)"
            ),
            {"g": grant, "i": item, "sha": SHA},
        )
        await c.execute(
            text(
                "INSERT INTO user_data_read (at, owner_subject, app, item_id, sha256,"
                " reader, operation, grant_id) VALUES (:at, 'alice', 'knowlab', :i,"
                " :sha, 'svc-bench', 'content', :g)"
            ),
            {"at": NOW, "i": item, "sha": SHA, "g": grant},
        )
        await c.execute(
            text(
                "INSERT INTO knowledge_dataset (dataset_id, data_version, title,"
                " bucket, object_key, sha256, size_bytes, start_date, end_date,"
                " source_app, status, registered_by, registered_at) VALUES"
                " ('sh600009', 'v1', '上海机场', 'b', 'k', :sha, 1, '2016-12-31',"
                " '2025-12-31', 'knowfeed', 'active', 'svc-feed', :at)"
            ),
            {"sha": SHA, "at": NOW},
        )
    return item


async def test_the_legacy_tables_go_and_items_and_reads_stay(at_0017) -> None:
    engine, migrate = at_0017
    before = await _shape(engine)
    assert {r[0] for r in before["columns"]} == set(TABLES)
    item = await _seed(engine)

    await migrate("upgrade")
    for table in TABLES:
        assert await _all(engine, "SELECT to_regclass(:t)", t=table) == [(None,)]
    # 资料与读数留痕都在，两栏 app 照旧（第四版在用）
    assert await _all(engine, "SELECT id, app FROM user_data_item") == [
        (item, "knowlab")
    ]
    assert await _all(engine, "SELECT item_id, app FROM user_data_read") == [
        (item, "knowlab")
    ]
    for table in ("user_data_run_grant", "user_data_use", "user_data_version"):
        assert await _all(engine, "SELECT to_regclass(:t) IS NOT NULL", t=table) == [
            (True,)
        ]

    # 退回：建回的空表与 0017 时一模一样，删掉的行不恢复
    await migrate("downgrade")
    assert await _shape(engine) == before
    for table in TABLES:
        assert await _all(engine, f"SELECT count(*) FROM {table}") == [(0,)]


def test_no_model_maps_a_dropped_table() -> None:
    from app.infrastructure.models import Base

    assert not set(TABLES) & set(Base.metadata.tables)
