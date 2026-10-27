"""资料库接口 `user-data/v2`（SDD 0043 块 1a）：走一遍 HTTP，库是真的。"""

from __future__ import annotations

import hashlib
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI
from test_durable_delivery_db import db as db
from test_user_data import CSV, Clock, MemoryObjects

from app.application.services.user_library_service import UserLibraryService
from app.domain.user_data import Owner
from app.infrastructure.repositories.user_library import SqlUserLibraryRepository
from app.interfaces.endpoints import user_library_routes
from app.interfaces.errors.exception_handlers import register_exception_handlers

BASE = "/api/internal/v2/user-data"


@pytest.fixture
def api(db):
    service = UserLibraryService(
        SqlUserLibraryRepository(lambda: db), MemoryObjects(), prefix="p", clock=Clock()
    )
    app = FastAPI()
    register_exception_handlers(app)
    app.include_router(user_library_routes.router, prefix="/api")
    state = {"owner": Owner("alice", "knowbench")}
    overrides = app.dependency_overrides
    overrides[user_library_routes.internal_host_only] = lambda: None
    overrides[user_library_routes.require_user_owner] = lambda: state["owner"]
    overrides[user_library_routes.get_user_library_service] = lambda: service
    state["service"] = "knowlab"
    caller = lambda: SimpleNamespace(caller=state["service"])  # noqa: E731
    overrides[user_library_routes.require_directory_service] = caller
    overrides[user_library_routes.require_purchase_service] = caller
    client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://knowbase"
    )
    return client, state


async def test_upload_describe_version_download_and_remove(api):
    client, state = api
    created = await client.post(
        f"{BASE}/materials",
        params={
            "name": "营收.csv",
            "subject": "600519.SH",
            "doc_type": "statistics",
            "confirm": "true",
        },
        content=CSV,
    )
    assert created.status_code == 201, created.text
    body = created.json()
    material_id = body["material"]["material_id"]
    assert body["category"] == "uploaded" and body["material"]["mine"] is True
    assert body["material"]["description"]["resolvable"] is True
    assert body["material"]["description"]["data_format"] == "csv"
    assert body["version"]["sha256"] == hashlib.sha256(CSV).hexdigest()

    listed = await client.get(f"{BASE}/materials", params={"category": "uploaded"})
    assert [e["material"]["material_id"] for e in listed.json()] == [material_id]
    assert (await client.get(f"{BASE}/materials", params={"category": "x"})).json()[
        "code"
    ] == "category_invalid"

    described = await client.put(
        f"{BASE}/materials/{material_id}/description",
        json={"subject": "000001.SZ", "confirm": False},
    )
    assert described.json()["material"]["description"]["status"] == "unconfirmed"

    added = await client.post(
        f"{BASE}/materials/{material_id}/versions", content=CSV + b"5,6\n"
    )
    assert added.status_code == 201 and added.json()["version"]["version_no"] == 2
    first = await client.get(f"{BASE}/materials/{material_id}/versions/1/content")
    assert first.content == CSV
    assert first.headers["X-Content-SHA256"] == hashlib.sha256(CSV).hexdigest()

    raised = await client.put(
        f"{BASE}/materials/{material_id}/license", json={"license": "sale"}
    )
    assert raised.json()["code"] == "attest_required"
    raised = await client.put(
        f"{BASE}/materials/{material_id}/license",
        json={"license": "sale", "attest": True},
    )
    assert raised.json()["material"]["license"] == "sale"
    history = await client.get(f"{BASE}/materials/{material_id}/history")
    assert [h["what"] for h in history.json()] == ["description", "license"]

    state["owner"] = Owner("bob", "knowbench")
    hidden = await client.get(f"{BASE}/materials/{material_id}")
    assert (hidden.status_code, hidden.json()["code"]) == (404, "item_not_found")
    state["owner"] = Owner("alice", "knowlab")  # 同一个人换个应用代办：资料不分区
    assert (await client.get(f"{BASE}/materials/{material_id}")).status_code == 200

    assert (await client.delete(f"{BASE}/materials/{material_id}")).status_code == 204
    assert (await client.get(f"{BASE}/materials")).json() == []


async def test_vocabulary(api):
    client, _ = api
    vocab = (await client.get(f"{BASE}/vocabulary")).json()
    assert vocab["version"] == 1
    assert vocab["doc_types"]["annual_report"] == "年报"
    assert "cn_security" in vocab["subject_kinds"]


async def test_bad_descriptions_are_named(api):
    client, _ = api
    refused = await client.post(
        f"{BASE}/materials", params={"name": "a.csv", "subject": "茅台"}, content=CSV
    )
    assert (refused.status_code, refused.json()["code"]) == (422, "subject_invalid")


async def _upload(client, name="营收.csv") -> str:
    created = await client.post(f"{BASE}/materials", params={"name": name}, content=CSV)
    assert created.status_code == 201, created.text
    return created.json()["material"]["material_id"]


async def test_show_hide_columns_and_account(api):
    """块 1b：展示即同意；默认展示要本人看过开关；应用只看自己那一栏。"""
    client, state = api
    account = (await client.get(f"{BASE}/account")).json()
    assert account == {
        "default_display": True,
        "seen": False,
        "displays_by_default": False,
    }
    quiet = await _upload(client, "没看过开关.csv")
    assert (await client.get(f"{BASE}/materials/{quiet}/uses")).json() == []

    account = await client.put(f"{BASE}/account", json={"default_display": True})
    assert account.json()["displays_by_default"] is True
    shown = await _upload(client, "看过以后.csv")
    uses = (await client.get(f"{BASE}/materials/{shown}/uses")).json()
    assert sorted(u["use"] for u in uses) == ["app:knowbench", "app:knowlab"]
    assert [
        e["material"]["material_id"]
        for e in (await client.get(f"{BASE}/columns/knowbench")).json()
    ] == [shown]

    made = await client.post(
        f"{BASE}/materials/{quiet}/uses", json={"use": "app:knowbench"}
    )
    assert made.status_code == 201 and made.json()["use"] == "app:knowbench"
    column = (await client.get(f"{BASE}/columns/knowbench")).json()
    assert sorted(e["material"]["material_id"] for e in column) == sorted(
        [quiet, shown]
    )
    bad = await client.post(f"{BASE}/materials/{quiet}/uses", json={"use": "app:x"})
    assert (bad.status_code, bad.json()["code"]) == (422, "use_invalid")

    gone = await client.delete(
        f"{BASE}/materials/{shown}/uses", params={"use": "app:knowbench"}
    )
    assert gone.status_code == 204
    column = (await client.get(f"{BASE}/columns/knowbench")).json()
    assert [e["material"]["material_id"] for e in column] == [quiet]

    bulk = await client.post(
        f"{BASE}/uses/bulk",
        json={"material_ids": [quiet, shown, "nope"], "use": "market", "on": True},
    )
    assert bulk.json() == {"done": 2}
    denied = await client.get(f"{BASE}/columns/knowmart")
    assert (denied.status_code, denied.json()["code"]) == (403, "column_denied")
    state["owner"] = Owner("alice", "knowmart")
    assert len((await client.get(f"{BASE}/columns/knowmart")).json()) == 2

    state["owner"] = Owner("bob", "knowbench")  # 别人的资料点不了
    other = await client.post(
        f"{BASE}/materials/{quiet}/uses", json={"use": "app:knowbench"}
    )
    assert other.status_code == 404


async def test_purchase_and_expert_directory(api):
    """服务接口：knowmart 记成交（不复制、只到成交那一版），knowlab 推名录。"""
    client, state = api
    material_id = await _upload(client)
    await client.put(
        f"{BASE}/materials/{material_id}/license",
        json={"license": "sale", "attest": True},
    )
    deal = {
        "seller": "alice",
        "material_id": material_id,
        "buyer": "bob",
        "transaction_ref": "T-1",
    }
    state["service"] = "knowlab"
    wrong = await client.post(f"{BASE}/purchases", json=deal)
    assert (wrong.status_code, wrong.json()["code"]) == (403, "service_scope_denied")
    state["service"] = "knowmart"
    bought = await client.post(f"{BASE}/purchases", json=deal)
    assert bought.status_code == 201, bought.text
    assert bought.json() == {
        "material_id": material_id,
        "buyer": "bob",
        "version_no": 1,
        "license": "self",
    }
    again = await client.post(f"{BASE}/purchases", json=deal)
    assert again.status_code == 201
    state["owner"] = Owner("bob", "knowbench")
    shelf = (
        await client.get(f"{BASE}/materials", params={"category": "purchased"})
    ).json()
    assert [e["material"]["material_id"] for e in shelf] == [material_id]

    state["service"] = "knowmart"
    wrong = await client.put(
        f"{BASE}/experts/e1", json={"owner": "bob", "name": "茅台分析"}
    )
    assert wrong.status_code == 403
    state["service"] = "knowlab"
    pushed = await client.put(
        f"{BASE}/experts/e1",
        json={"owner": "bob", "name": "茅台分析", "latest_version": "1.0"},
    )
    assert pushed.json() == {
        "expert_id": "e1",
        "name": "茅台分析",
        "latest_version": "1.0",
    }
    moved = await client.put(f"{BASE}/experts/e1", json={"owner": "carol", "name": "x"})
    assert (moved.status_code, moved.json()["code"]) == (409, "expert_owner_changed")
    assert [e["expert_id"] for e in (await client.get(f"{BASE}/experts")).json()] == [
        "e1"
    ]
    state["owner"] = Owner("alice", "knowbench")
    assert (await client.get(f"{BASE}/experts")).json() == []
