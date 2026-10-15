"""数据集登记表：真数据库（含本次迁移）。"""

from __future__ import annotations

import asyncio

import pytest
from test_durable_delivery_db import db as db
from test_durable_delivery_db import sql
from test_knowledge_datasets import BUCKET, KEY, SHA256, VERSION, registration

from app.domain.datasets import ACTIVE, SUPERSEDED, DatasetVersionConflict
from app.infrastructure.repositories.dataset_registry import SqlDatasetRegistry

NEWER = "sh600009-financials-ffffffffffffffff"
BY = "service:knowfeed-backend"


def registry(db) -> SqlDatasetRegistry:
    sessions = db  # 夹具的会话就是生产的那一种
    return SqlDatasetRegistry(lambda: sessions)


def newer():
    return registration(
        data_version=NEWER,
        sha256="f" * 64,
        object_key=KEY.replace(VERSION, NEWER),
        end_date="2026-09-30",
    )


async def test_a_registration_is_stored_and_becomes_the_active_version(db):
    entity = await registry(db).register(registration(), registered_by=BY)
    assert (entity.dataset_id, entity.data_version, entity.status) == (
        "sh600009-financials",
        VERSION,
        ACTIVE,
    )
    assert (entity.bucket, entity.object_key, entity.sha256) == (BUCKET, KEY, SHA256)
    assert entity.registered_by == BY and entity.registered_at.tzinfo is not None
    assert (
        await sql(
            db,
            "SELECT status || '/' || security_code || '/' || source_app || '/' || "
            "source_ref || '/' || registered_by FROM knowledge_dataset",
        )
        == f"active/600009/knowfeed/ingestion-1/{BY}"
    )
    active = await registry(db).active()
    assert [d.data_version for d in active] == [VERSION]
    assert (await registry(db).get_active("sh600009-financials")) == active[0]
    assert await registry(db).get_active("sh600519-financials") is None


async def test_registering_the_same_version_again_adds_nothing(db):
    first = await registry(db).register(registration(), registered_by=BY)
    again = await registry(db).register(registration(), registered_by="service:other")
    assert again == first  # 第一次登记的人和时间不被改写
    assert await sql(db, "SELECT count(*) FROM knowledge_dataset") == 1


async def test_a_newer_version_supersedes_the_older_one(db):
    await registry(db).register(registration(), registered_by=BY)
    entity = await registry(db).register(newer(), registered_by=BY)
    assert entity.data_version == NEWER
    assert [d.data_version for d in await registry(db).active()] == [NEWER]
    assert (
        await sql(
            db,
            "SELECT string_agg(data_version || '=' || status, ',' "
            "ORDER BY registered_at) FROM knowledge_dataset",
        )
        == f"{VERSION}=superseded,{NEWER}=active"
    )


async def test_registering_an_older_version_again_makes_it_active_again(db):
    await registry(db).register(registration(), registered_by=BY)
    await registry(db).register(newer(), registered_by=BY)
    entity = await registry(db).register(registration(), registered_by=BY)
    assert (entity.data_version, entity.status) == (VERSION, ACTIVE)
    assert [d.data_version for d in await registry(db).active()] == [VERSION]
    assert await sql(db, "SELECT count(*) FROM knowledge_dataset") == 2
    assert (
        await sql(db, "SELECT count(*) FROM knowledge_dataset WHERE status='active'")
        == 1
    )


@pytest.mark.parametrize(
    "changes",
    [
        {"sha256": "a" * 64},
        {"object_key": KEY.replace("sh600009-financials.sqlite", "other.sqlite")},
        {"bucket": "another-bucket"},
    ],
)
async def test_the_same_version_with_other_content_is_refused(db, changes):
    await registry(db).register(registration(), registered_by=BY)
    with pytest.raises(DatasetVersionConflict):
        await registry(db).register(registration(**changes), registered_by=BY)
    assert (
        await sql(db, "SELECT sha256 || '/' || status FROM knowledge_dataset")
        == f"{SHA256}/active"
    )


async def test_datasets_do_not_affect_each_other(db):
    await registry(db).register(registration(), registered_by=BY)
    other = registration(
        dataset_id="sh600519-financials",
        data_version="sh600519-financials-0000000000000001",
        security_code="600519",
        title="贵州茅台 财务报表",
        sha256="b" * 64,
    )
    await registry(db).register(other, registered_by=BY)
    assert [d.dataset_id for d in await registry(db).active()] == [
        "sh600009-financials",
        "sh600519-financials",
    ]


async def test_concurrent_registrations_leave_exactly_one_active_version(db):
    results = await asyncio.gather(
        registry(db).register(registration(), registered_by=BY),
        registry(db).register(newer(), registered_by=BY),
        registry(db).register(registration(), registered_by=BY),
        registry(db).register(newer(), registered_by=BY),
    )
    assert {r.data_version for r in results} == {VERSION, NEWER}
    assert await sql(db, "SELECT count(*) FROM knowledge_dataset") == 2
    assert (
        await sql(db, "SELECT count(*) FROM knowledge_dataset WHERE status='active'")
        == 1
    )


async def test_the_database_refuses_two_active_versions_and_bad_values(db):
    await registry(db).register(registration(), registered_by=BY)
    insert = (
        "INSERT INTO knowledge_dataset (dataset_id, data_version, title, bucket,"
        " object_key, sha256, size_bytes, start_date, end_date, source_app, status,"
        " registered_by, registered_at) VALUES ('sh600009-financials', :v, 't', 'b',"
        " 'k', :s, 1, '2020-01-01', '2020-12-31', 'knowfeed', :status, 'x', now())"
    )
    with pytest.raises(Exception, match="uq_knowledge_dataset_one_active"):
        await sql(db, insert, v="v2", s="a" * 64, status="active")
    with pytest.raises(Exception, match="ck_knowledge_dataset_status"):
        await sql(db, insert, v="v3", s="a" * 64, status="draft")
    with pytest.raises(Exception, match="ck_knowledge_dataset_sha256"):
        await sql(db, insert, v="v4", s="A" * 64, status="superseded")
    with pytest.raises(Exception, match="uq_knowledge_dataset_version"):
        await sql(db, insert, v=VERSION, s="a" * 64, status="superseded")


async def test_the_history_keeps_every_version(db):
    """管理端的登记表（`AT-KNOW-02`）：现行的与被取代的都在，被取代的有时间。"""
    await registry(db).register(registration(), registered_by=BY)
    await registry(db).register(newer(), registered_by=BY)
    other = registration(
        dataset_id="sh600519-financials",
        data_version="sh600519-financials-0000000000000001",
        security_code="600519",
        title="贵州茅台 财务报表",
        sha256="b" * 64,
    )
    await registry(db).register(other, registered_by=BY)

    everything = await registry(db).versions()
    assert [(d.dataset_id, d.data_version, d.status) for d in everything] == [
        ("sh600009-financials", NEWER, ACTIVE),
        ("sh600009-financials", VERSION, SUPERSEDED),
        ("sh600519-financials", "sh600519-financials-0000000000000001", ACTIVE),
    ]
    one = await registry(db).versions("sh600009-financials")
    assert [d.data_version for d in one] == [NEWER, VERSION]
    old = one[1]
    assert old.changed_at is not None and old.changed_at >= old.registered_at
    assert await registry(db).versions("sh000001-financials") == []
