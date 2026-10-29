"""迁移 20261029_0019：正文、按页文字两种读法存进 PostgreSQL（SDD 0043 第五节）。

建 `user_data_text`、`user_data_page`，删 `user_data_rendition.object_key`；
万一有存成对象的正文、按页文字读法，标成没成、写明原因，表格读法不动。
真 PostgreSQL、一次性 schema：先迁到 0018 造数，再单跑这一个迁移的 upgrade 与
downgrade。
"""

from __future__ import annotations

import os
import uuid
from datetime import UTC, datetime

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import create_async_engine
from test_drop_legacy_data_db import _all, _run
from test_durable_delivery_db import ROOT

TARGET = ROOT / "alembic/versions/20261029_0019_readings_in_postgres.py"
BEFORE = [
    p
    for p in sorted((ROOT / "alembic/versions").glob("20*.py"))
    if p.name < TARGET.name
]
NOW = datetime(2026, 10, 29, 12, 0, tzinfo=UTC)
SHA = "a" * 64


@pytest_asyncio.fixture
async def at_0018():
    url = os.environ.get("DELIVERY_TEST_DATABASE_URL")
    if not url:
        pytest.skip("set DELIVERY_TEST_DATABASE_URL to a disposable *_tests database")
    parsed = make_url(url)
    assert parsed.database and parsed.database.endswith("_tests")
    schema = "readings_test_" + uuid.uuid4().hex
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


async def _seed(engine) -> dict[str, uuid.UUID]:
    """0018 时的样子：一份资料的第 1 版，带存成对象的正文与按页文字、一个表格读法。"""
    item, version = uuid.uuid4(), uuid.uuid4()
    ids = {kind: uuid.uuid4() for kind in ("text", "pages", "table")}
    async with engine.begin() as c:
        await c.execute(
            text(
                "INSERT INTO user_data_item (id, owner_subject, app, name, media_type,"
                " size_bytes, sha256, origin, object_key, created_at, no_resale)"
                " VALUES (:id, 'alice', 'knowlab', 'a.pdf', 'application/pdf', 10,"
                " :sha, 'upload', 'k/a', :at, false)"
            ),
            {"id": item, "sha": SHA, "at": NOW},
        )
        await c.execute(
            text(
                "INSERT INTO user_data_version (id, item_id, version_no, sha256,"
                " size_bytes, media_type, object_key, created_at, created_by)"
                " VALUES (:id, :item, 1, :sha, 10, 'application/pdf', 'k/a/1', :at,"
                " 'alice')"
            ),
            {"id": version, "item": item, "sha": SHA, "at": NOW},
        )
        for kind, rendition in ids.items():
            await c.execute(
                text(
                    "INSERT INTO user_data_rendition (id, version_id, kind,"
                    " rendition_no, processor, processor_version, status, sha256,"
                    " object_key, detail, created_at, ready_at) VALUES (:id, :v,"
                    " :kind, 1, 'knowfeed', '0.1', 'ready', :sha, :key, '{}', :at,"
                    " :at)"
                ),
                {
                    "id": rendition,
                    "v": version,
                    "kind": kind,
                    "sha": SHA,
                    "key": None if kind == "table" else f"k/a/1/renditions/{kind}",
                    "at": NOW,
                },
            )
    return ids


async def test_readings_move_into_tables_and_object_keys_go(at_0018) -> None:
    engine, migrate = at_0018
    ids = await _seed(engine)

    await migrate("upgrade")
    for table in ("user_data_text", "user_data_page"):
        assert await _all(engine, "SELECT to_regclass(:t) IS NOT NULL", t=table) == [
            (True,)
        ]
    assert await _all(
        engine,
        "SELECT count(*) FROM information_schema.columns"
        " WHERE table_schema = current_schema()"
        " AND table_name = 'user_data_rendition' AND column_name = 'object_key'",
    ) == [(0,)]
    rows = dict(
        (r[0], r[1:])
        for r in await _all(
            engine,
            "SELECT kind, status, ready_at IS NULL, detail->>'reason'"
            " FROM user_data_rendition",
        )
    )
    reason = "stored as an object before 0019"
    assert rows == {
        "text": ("failed", True, reason),
        "pages": ("failed", True, reason),
        "table": ("ready", False, None),
    }
    # 页码从 1 起，一版一页一行
    async with engine.begin() as c:
        await c.execute(
            text("INSERT INTO user_data_page VALUES (:r, 1, '封面')"),
            {"r": ids["pages"]},
        )
    with pytest.raises(Exception, match="ck_user_data_page_no"):
        async with engine.begin() as c:
            await c.execute(
                text("INSERT INTO user_data_page VALUES (:r, 0, 'x')"),
                {"r": ids["pages"]},
            )
    async with engine.begin() as c:
        await c.execute(text("DELETE FROM user_data_page"))

    await migrate("downgrade")
    for table in ("user_data_text", "user_data_page"):
        assert await _all(engine, "SELECT to_regclass(:t)", t=table) == [(None,)]
    assert await _all(engine, "SELECT count(object_key) FROM user_data_rendition") == [
        (0,)
    ]
