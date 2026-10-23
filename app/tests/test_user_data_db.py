"""用户资料的记录：真数据库（含本次迁移，SDD 0042）。"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.exc import IntegrityError
from test_durable_delivery_db import db as db
from test_durable_delivery_db import sql
from test_user_data import ALICE, ALICE_LAB, BOB, CSV, Clock, MemoryObjects, pin

from app.application.services.user_data_service import UserDataService
from app.domain.user_data import (
    Consent,
    Grant,
    Item,
    Pin,
    ReadEvent,
    UserDataError,
)
from app.infrastructure.repositories.user_data import SqlUserDataRepository

NOW = datetime(2026, 10, 23, 12, 0, tzinfo=UTC)
SHA = "a" * 64


def repo(db) -> SqlUserDataRepository:
    sessions = db  # 夹具的会话就是生产的那一种
    return SqlUserDataRepository(lambda: sessions)


def item(owner=ALICE, *, origin="upload", origin_ref=None, size=10, sha=SHA) -> Item:
    item_id = str(uuid.uuid4())
    return Item(
        id=item_id,
        owner=owner,
        name="a.csv",
        media_type="text/csv; charset=utf-8",
        size_bytes=size,
        sha256=sha,
        origin=origin,
        origin_ref=origin_ref,
        object_key=f"users/x/{owner.app}/{item_id}",
        created_at=NOW,
    )


def grant(items, *, expires=NOW + timedelta(hours=1), owner=ALICE) -> Grant:
    return Grant(
        id=str(uuid.uuid4()),
        kind="delegation",
        owner=owner,
        reader_app="knowbench",
        ref="task-1",
        pins=tuple(Pin(i.id, i.sha256) for i in items),
        scope="content",
        issued_by="alice",
        issued_at=NOW,
        expires_at=expires,
    )


async def refused(work, status: int, code: str) -> None:
    with pytest.raises(UserDataError) as caught:
        await work
    assert (caught.value.status, caught.value.code) == (status, code)


async def test_items_round_trip_per_partition(db):
    r = repo(db)
    a = await r.add_item(item(size=10))
    await r.add_item(item(size=5))
    await r.add_item(item(ALICE_LAB, size=7))
    await r.add_item(item(BOB, size=3))
    got = await r.get_item(a.id)
    assert got is not None and (got.owner, got.size_bytes, got.sha256) == (
        ALICE,
        10,
        SHA,
    )
    assert len(await r.list_items(ALICE)) == 2
    assert await r.bytes_used(ALICE) == 15 and await r.bytes_used(ALICE_LAB) == 7
    assert await r.get_item("not-a-uuid") is None
    assert set(await r.items([a.id, "bad", str(uuid.uuid4())])) == {a.id}


async def test_one_copy_per_origin_ref_in_a_partition(db):
    r = repo(db)
    bought = await r.add_item(item(origin="purchase", origin_ref="tx-1"))
    assert (await r.find_by_origin(ALICE, "tx-1")).id == bought.id
    assert await r.find_by_origin(ALICE_LAB, "tx-1") is None
    with pytest.raises(IntegrityError):  # 唯一索引挡住第二份
        await r.add_item(item(origin="purchase", origin_ref="tx-1"))
    await r.add_item(item(ALICE_LAB, origin="purchase", origin_ref="tx-1"))


async def test_delete_is_refused_while_a_grant_or_consent_uses_the_item(db):
    r = repo(db)
    a = await r.add_item(item())
    g = await r.add_grant(grant([a]))
    await refused(r.delete_item(a.id, NOW), 409, "in_use")
    await r.revoke_grant(g.id, NOW)
    lab = await r.add_item(item(ALICE_LAB))
    c = await r.add_consent(
        Consent(
            id=str(uuid.uuid4()),
            owner=ALICE_LAB,
            purpose="knowlab-test",
            ref="idea-1@v1",
            pins=(Pin(lab.id, lab.sha256),),
            created_at=NOW,
        )
    )
    await refused(r.delete_item(lab.id, NOW), 409, "in_use")
    await r.withdraw_consent(c.id, NOW)
    await r.delete_item(lab.id, NOW)
    await r.delete_item(a.id, NOW)
    assert (await r.get_item(a.id)).deleted_at is not None
    assert await r.list_items(ALICE) == []
    await refused(r.delete_item(a.id, NOW), 404, "item_not_found")
    # 过期的授权不挡
    b = await r.add_item(item())
    await r.add_grant(grant([b], expires=NOW + timedelta(seconds=1)))
    await r.delete_item(b.id, NOW + timedelta(seconds=2))


async def test_grants_refuse_deleted_or_changed_items_and_round_trip(db):
    r = repo(db)
    a, b = await r.add_item(item()), await r.add_item(item(sha="b" * 64))
    g = await r.add_grant(grant([a, b]))
    back = await r.get_grant(g.id)
    assert back is not None and set(back.pins) == set(g.pins)
    assert (back.kind, back.owner, back.scope, back.revoked_at) == (
        "delegation",
        ALICE,
        "content",
        None,
    )
    changed = Grant(**{**grant([a]).__dict__, "pins": (Pin(a.id, "c" * 64),)})
    await refused(r.add_grant(changed), 409, "sha256_mismatch")
    c = await r.add_item(item())
    await r.delete_item(c.id, NOW)
    await refused(r.add_grant(grant([c])), 404, "item_not_found")
    await r.revoke_grant(g.id, NOW)
    assert (await r.get_grant(g.id)).revoked_at == NOW


async def test_reads_are_logged_without_content(db):
    r = repo(db)
    a = await r.add_item(item())
    await r.record_read(
        ReadEvent(
            owner=ALICE,
            item_id=a.id,
            sha256=a.sha256,
            reader="svc-bench",
            operation="content",
            grant_id=str(uuid.uuid4()),
            at=NOW,
        )
    )
    row = await sql(
        db, "SELECT reader || '|' || operation || '|' || sha256 FROM user_data_read"
    )
    assert row == f"svc-bench|content|{SHA}"


async def test_the_service_on_a_real_database(db):
    """上传、同意依据、knowlab 签测试授权、工作台凭授权读：整条走一遍。"""
    clock = Clock()
    service = UserDataService(repo(db), MemoryObjects(), prefix="users", clock=clock)
    data = await service.upload(ALICE_LAB, "营收.csv", CSV)
    consent = await service.consent(
        ALICE_LAB, "knowlab-test", "idea-1@v2", (pin(data),)
    )
    test = await service.grant_test(
        "svc-knowlab", "alice", consent.id, "knowbench", "test-5", (pin(data),)
    )
    _, content = await service.read(
        "knowbench", "svc-bench", test.id, data.id, "content"
    )
    assert content == CSV
    await refused(service.delete(ALICE_LAB, data.id), 409, "in_use")
