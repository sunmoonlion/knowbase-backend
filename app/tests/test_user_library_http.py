"""资料库接口 `user-data/v2`（SDD 0043 块 1a）：走一遍 HTTP，库是真的。"""

from __future__ import annotations

import hashlib

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
