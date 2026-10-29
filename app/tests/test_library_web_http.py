"""knowbase web端自家的资料库接口与管理端（SDD 0044）：走一遍 HTTP，库是真的。

web端凭本人会话、管理端凭管理会话，复用 `user-data/v2` 的同一套服务；另核两条新的
服务接口：knowmart 成批核条目、knowfeed 生成新需求前核订阅范围同意。
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI
from test_durable_delivery_db import db as db
from test_user_data import CSV, Clock, MemoryObjects

from app.application.services.user_library_service import UserLibraryService
from app.application.services.user_run_service import UserRunService
from app.infrastructure.external.first_page import read_first_page
from app.infrastructure.repositories.user_library import SqlUserLibraryRepository
from app.infrastructure.repositories.user_runs import SqlUserRunRepository
from app.interfaces.endpoints import (
    library_web_routes,
    user_data_admin_routes,
    user_library_routes,
    user_run_routes,
)
from app.interfaces.errors.exception_handlers import register_exception_handlers
from app.main import app as real_app
from core.config import Settings

WEB = "/api/web/v1/library"
ADMIN = "/api/admin/v1/user-data"
INTERNAL = "/api/internal/v2/user-data"
SOURCES = '{"knowbench": {}, "knowfeed": {}}'


@pytest.fixture
def api(db):
    objects, clock = MemoryObjects(), Clock()
    service = UserLibraryService(
        SqlUserLibraryRepository(lambda: db),
        objects,
        prefix="p",
        clock=clock,
        first_page=read_first_page,
    )
    runs = UserRunService(
        SqlUserLibraryRepository(lambda: db),
        SqlUserRunRepository(lambda: db),
        objects,
        prefix="p",
        clock=clock,
    )
    app = FastAPI()
    register_exception_handlers(app)
    for module in (
        library_web_routes,
        user_data_admin_routes,
        user_library_routes,
        user_run_routes,
    ):
        app.include_router(module.router, prefix="/api")
    state = {"user": "alice", "admin": "ops-1", "service": "knowmart"}
    services = SimpleNamespace(library=service, runs=runs)
    overrides = app.dependency_overrides
    overrides[library_web_routes.get_web_current_user] = lambda: SimpleNamespace(
        subject=state["user"]
    )
    overrides[library_web_routes.web_library_settings] = lambda: Settings(
        _env_file=None, cross_app_sources_json=SOURCES
    )
    overrides[user_data_admin_routes.require_knowbase_admin] = lambda: SimpleNamespace(
        subject=state["admin"]
    )
    overrides[user_data_admin_routes.require_dispose] = lambda: SimpleNamespace(
        subject=state["admin"]
    )
    overrides[user_library_routes.internal_host_only] = lambda: None
    overrides[user_library_routes.get_user_library_service] = lambda: service
    overrides[user_run_routes.get_user_run_service] = lambda: runs
    caller = lambda: SimpleNamespace(  # noqa: E731
        caller=state["service"], principal=SimpleNamespace(subject="svc-1")
    )
    overrides[user_library_routes.require_purchase_service] = caller
    overrides[user_library_routes.require_supply_service] = caller
    overrides[user_library_routes.require_directory_service] = caller
    overrides[user_library_routes.require_subscribing_owner] = lambda: SimpleNamespace(
        subject=state["user"], app="knowfeed"
    )
    overrides[user_run_routes.require_grant_test_service] = caller
    client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://knowbase"
    )
    return client, state, services


async def _upload(client, name="营收.csv", **params) -> dict:
    created = await client.post(
        f"{WEB}/materials", params={"name": name, **params}, content=CSV
    )
    assert created.status_code == 201, created.text
    return created.json()


async def test_recognize_then_upload_with_the_confirmed_description(api):
    client, _, _ = api
    usage = (await client.get(f"{WEB}/usage")).json()
    assert usage["bytes_used"] == 0 and usage["quota_bytes"] == 1024**3
    assert usage["max_item_bytes"] == 50 * 1024 * 1024
    assert ".pdf" in usage["accepted"] and ".md" in usage["accepted"]
    vocabulary = (await client.get(f"{WEB}/vocabulary")).json()
    assert vocabulary["doc_types"]["research_company"]["class"] == "research"

    page = "证券研究报告\n华泰证券研究所\n分析师：王五\n投资评级：增持\n2026年10月20日"
    found = await client.post(
        f"{WEB}/recognitions",
        params={"name": "宁德时代（300750）公司点评.txt"},
        content=page.encode(),
    )
    assert found.status_code == 200, found.text
    body = found.json()
    assert body["rules_version"] == 1 and body["read_first_page"] is True
    d = body["description"]
    assert (d["subject"], d["doc_type"], d["doc_class"]) == (
        "300750.SZ",
        "research_company",
        "research",
    )
    assert d["attributes"]["institution"] == "华泰证券"
    assert d["attributes"]["rating"] == "增持"
    bad = await client.post(
        f"{WEB}/recognitions", params={"name": "x.exe"}, content=b"MZ"
    )
    assert (bad.status_code, bad.json()["code"]) == (422, "type_not_allowed")

    # 本人在表单上确认（改了评级），才算确认过；研报的几维随上传一起写
    created = await client.post(
        f"{WEB}/materials",
        params={
            "name": "宁德时代（300750）公司点评.txt",
            "subject": "300750.SZ",
            "doc_type": "research_company",
            "disclosed_on": "2026-10-20",
            "attributes": json.dumps(
                {"institution": "华泰证券", "rating": "买入"}, ensure_ascii=False
            ),
            "confirm": "true",
        },
        content=page.encode(),
    )
    assert created.status_code == 201, created.text
    entry = created.json()
    assert entry["material"]["description"]["status"] == "confirmed"
    assert entry["material"]["description"]["attributes"]["rating"] == "买入"
    assert entry["shown"] == []
    material_id = entry["material"]["material_id"]
    history = (await client.get(f"{WEB}/materials/{material_id}/history")).json()
    assert history[0]["what"] == "recognized"
    recognized = history[0]["after"]
    assert recognized["rules_version"] == 1 and recognized["confirmed"] is True
    assert recognized["values"]["attributes"]["rating"] == "增持"
    broken = await client.post(
        f"{WEB}/materials", params={"name": "a.csv", "attributes": "["}, content=CSV
    )
    assert (broken.status_code, broken.json()["code"]) == (422, "attributes_invalid")


async def test_raising_the_license_at_upload_needs_and_logs_the_attestation(api):
    client, _, _ = api
    denied = await client.post(
        f"{WEB}/materials", params={"name": "a.csv", "license": "sale"}, content=CSV
    )
    assert (denied.status_code, denied.json()["code"]) == (422, "attest_required")
    entry = await _upload(client, "a.csv", license="sale", attest="true")
    material_id = entry["material"]["material_id"]
    assert entry["material"]["license"] == "sale"
    history = (await client.get(f"{WEB}/materials/{material_id}/history")).json()
    assert [h["after"] for h in history if h["what"] == "license"] == [
        {"license": "sale", "attested": True, "at": "upload"}
    ]


async def test_direct_upload_shows_to_the_source_app_in_the_same_upload(api):
    """直达上传（0044 第 2.4 节）：来源应用认得就顺带展示给它；认不得当作没带。"""
    client, _, _ = api
    entry = await _upload(client, "缺的那份.csv", **{"from": "knowbench", "ref": "t:1"})
    assert entry["shown"] == ["app:knowbench"]
    material_id = entry["material"]["material_id"]
    uses = (await client.get(f"{WEB}/materials/{material_id}/uses")).json()
    assert [(u["use"], u["created_via"]) for u in uses] == [
        ("app:knowbench", "direct-upload")
    ]
    # knowfeed 认得（能回跳）但没有知识库栏；没登记的应用整个当作没带
    assert (await _upload(client, "b.csv", **{"from": "knowfeed"}))["shown"] == []
    assert (await _upload(client, "c.csv", **{"from": "evil"}))["shown"] == []
    # 账号默认展示开着、本人点过：默认的两个加上来源应用，不重复
    await client.put(f"{WEB}/account", json={"default_display": True})
    both = await _upload(client, "d.csv", **{"from": "knowbench"})
    assert sorted(both["shown"]) == ["app:knowbench", "app:knowlab"]


async def test_shelf_detail_buttons_impact_and_delete(api):
    client, state, services = api
    entry = await _upload(client)
    material_id = entry["material"]["material_id"]
    shown = await client.post(
        f"{WEB}/materials/{material_id}/uses", json={"use": "market"}
    )
    assert shown.status_code == 201
    listed = (
        await client.get(f"{WEB}/materials", params={"category": "uploaded"})
    ).json()
    assert [(e["material"]["material_id"], e["shown"]) for e in listed] == [
        (material_id, ["market"])
    ]
    impact = (await client.get(f"{WEB}/materials/{material_id}/impact")).json()
    assert impact == {
        "other_holders": 0,
        "market": True,
        "bound_experts": [],
        "frozen_experts": [],
        "active_grants": 0,
    }
    versions = (await client.get(f"{WEB}/materials/{material_id}/versions")).json()
    assert [v["version_no"] for v in versions] == [1]
    added = await client.post(
        f"{WEB}/materials/{material_id}/versions", content=CSV + b"2024,11\n"
    )
    assert added.json()["version"]["version_no"] == 2
    got = await client.get(f"{WEB}/materials/{material_id}/versions/1/content")
    assert got.content == CSV and got.headers["Content-Disposition"] == "attachment"
    reads = (await client.get(f"{WEB}/materials/{material_id}/reads")).json()
    assert [(r["operation"], r["app"]) for r in reads] == [
        ("owner-download", "knowbase")
    ]
    renditions = await client.get(
        f"{WEB}/materials/{material_id}/versions/1/renditions"
    )
    assert renditions.json() == []
    later = await client.post(f"{WEB}/materials/{material_id}/versions/2/processing")
    assert later.status_code == 201 and later.json()["version_no"] == 2

    state["user"] = "bob"  # 别人的看不到、删不了
    assert (await client.get(f"{WEB}/materials/{material_id}")).status_code == 404
    assert (await client.delete(f"{WEB}/materials/{material_id}")).status_code == 404
    state["user"] = "alice"
    assert (await client.delete(f"{WEB}/materials/{material_id}")).status_code == 204
    assert (await client.get(f"{WEB}/materials")).json() == []


async def test_platform_consents_and_subscriptions_can_be_withdrawn_here(api):
    client, state, services = api
    material_id = (await _upload(client))["material"]["material_id"]
    # knowlab 凭本人令牌留下平台孵化的依据（这里直接走服务）
    await services.library.consent_platform(
        "alice", "knowlab", "incubate", "i-7", [(material_id, None)]
    )
    listed = (await client.get(f"{WEB}/platform-consents")).json()
    assert len(listed) == 1
    consent = listed[0]
    assert (consent["app"], consent["kind"], consent["ref"]) == (
        "knowlab",
        "incubate",
        "i-7",
    )
    assert consent["items"] == [{"material_id": material_id, "version_no": 1}]
    assert consent["operator_reads"] == 0 and consent["until"] is None
    gone = await client.delete(f"{WEB}/platform-consents/knowlab/incubate/i-7")
    assert gone.status_code == 204
    assert (await client.get(f"{WEB}/platform-consents")).json() == []

    scope = {
        "by": "url",
        "starts_on": "2026-10-27",
        "ends_on": "2027-04-27",
        "purpose": "看央行公告",
        "urls": ["https://www.pbc.gov.cn/rss"],
    }
    made = await client.put(f"{INTERNAL}/subscriptions/s-1/revisions/1", json=scope)
    assert made.status_code == 200, made.text
    assert [
        s["subscription_id"] for s in (await client.get(f"{WEB}/subscriptions")).json()
    ] == ["s-1"]
    state["service"] = "knowfeed"
    check = f"{INTERNAL}/service/subscriptions/s-1/revisions/1"
    live = await client.get(check, params={"subject": "alice"})
    assert live.status_code == 200 and live.json()["purpose"] == "看央行公告"
    # 本人在 knowbase 撤了，knowfeed 再核就是 404，不再生成新需求
    gone = await client.delete(f"{WEB}/subscriptions/s-1/revisions/1")
    assert gone.status_code == 204
    assert (await client.get(f"{WEB}/subscriptions")).json() == []
    after = await client.get(check, params={"subject": "alice"})
    assert (after.status_code, after.json()["code"]) == (404, "subscription_not_found")
    state["service"] = "knowmart"
    denied = await client.get(check, params={"subject": "alice"})
    assert (denied.status_code, denied.json()["code"]) == (403, "service_scope_denied")


async def test_output_converts_to_data_and_account_settings(api):
    client, state, services = api
    account = (await client.get(f"{WEB}/account")).json()
    assert account == {
        "default_display": True,
        "seen": False,
        "displays_by_default": False,
    }
    acked = await client.put(f"{WEB}/account", json={"default_display": False})
    assert acked.json() == {
        "default_display": False,
        "seen": True,
        "displays_by_default": False,
    }
    assert (await client.get(f"{WEB}/experts")).json() == []

    output, _ = await services.runs.write_output(
        "knowbench", "alice", "task-1", "run-1", "结论.md", "# 结论\n".encode()
    )
    outputs = (
        await client.get(f"{WEB}/materials", params={"category": "output"})
    ).json()
    assert [e["material"]["material_id"] for e in outputs] == [output.id]
    converted = await client.post(
        f"{WEB}/materials/{output.id}/convert", json={"name": "结论（数据）.md"}
    )
    assert converted.status_code == 201, converted.text
    body = converted.json()
    assert body["category"] == "uploaded"
    assert body["material"]["provenance"]["converted_from"] == output.id


async def test_market_offer_states_for_knowmart(api):
    client, state, services = api
    on_sale = (await _upload(client, "卖.csv", license="sale", attest="true"))[
        "material"
    ]["material_id"]
    lowered = (await _upload(client, "改低.csv", license="sale", attest="true"))[
        "material"
    ]["material_id"]
    not_handed = (await _upload(client, "没交.csv", license="sale", attest="true"))[
        "material"
    ]["material_id"]
    deleted = (await _upload(client, "删了.csv", license="sale", attest="true"))[
        "material"
    ]["material_id"]
    for material_id in (on_sale, lowered, deleted):
        await client.post(f"{WEB}/materials/{material_id}/uses", json={"use": "market"})
    await client.put(f"{WEB}/materials/{lowered}/license", json={"license": "self"})
    await client.delete(f"{WEB}/materials/{deleted}")
    items = [
        {"seller": "alice", "material_id": on_sale},
        {"seller": "alice", "material_id": lowered},
        {"seller": "alice", "material_id": not_handed},
        {"seller": "alice", "material_id": deleted},
        {"seller": "bob", "material_id": on_sale},
        {"seller": "alice", "material_id": "not-a-uuid"},
    ]
    got = await client.post(
        f"{INTERNAL}/service/market-offers/check", json={"items": items}
    )
    assert got.status_code == 200, got.text
    assert [i["state"] for i in got.json()["items"]] == [
        "offered",
        "not_for_sale",
        "gone",
        "gone",
        "gone",
        "gone",
    ]
    assert got.json()["items"][0] == {**items[0], "state": "offered"}
    empty = await client.post(
        f"{INTERNAL}/service/market-offers/check", json={"items": []}
    )
    assert empty.status_code == 422
    state["service"] = "knowlab"
    denied = await client.post(
        f"{INTERNAL}/service/market-offers/check", json={"items": items[:1]}
    )
    assert (denied.status_code, denied.json()["code"]) == (403, "service_scope_denied")


async def test_admin_usage_vocabulary_and_disabled_materials(api):
    client, state, services = api
    material_id = (await _upload(client))["material"]["material_id"]
    state["user"] = "bob"
    await _upload(client, "b.csv", confirm="true", doc_type="other_table")
    usage = (await client.get(f"{ADMIN}/usage")).json()
    assert usage["quota_bytes"] == 1024**3
    assert {u["subject"]: u["materials"] for u in usage["users"]} == {
        "alice": 1,
        "bob": 1,
    }
    vocabulary = (await client.get(f"{ADMIN}/vocabulary")).json()
    assert vocabulary["version"] == vocabulary["vocabulary"]["version"] == 2
    assert {
        (s["status"], s["doc_type"], s["count"]) for s in vocabulary["descriptions"]
    } == {("unconfirmed", None, 1), ("confirmed", "other_table", 1)}

    nothing = await client.get(f"{ADMIN}/materials")
    assert (nothing.status_code, nothing.json()["code"]) == (422, "filter_required")
    found = (await client.get(f"{ADMIN}/materials", params={"owner": "alice"})).json()
    assert [m["material_id"] for m in found] == [material_id]
    disabled = await client.post(
        f"{ADMIN}/materials/{material_id}/disable", json={"reason": "违规内容"}
    )
    assert disabled.status_code == 204
    listed = (
        await client.get(f"{ADMIN}/materials", params={"disabled": "true"})
    ).json()
    assert [m["material_id"] for m in listed] == [material_id]
    assert listed[0]["disabled_at"] is not None
    state["user"] = "alice"  # 禁用了，本人也看不到了
    assert (await client.get(f"{WEB}/materials/{material_id}")).status_code == 404
    purged = await client.post(
        f"{ADMIN}/materials/{material_id}/purge", json={"reason": "依法删除"}
    )
    assert purged.json() == {"purged": True, "result": "purged"}
    events = (await client.get(f"{ADMIN}/materials/{material_id}/deletions")).json()
    assert [(e["action"], e["reason"]) for e in events] == [
        ("disable", "违规内容"),
        ("purge", "依法删除"),
    ]


async def test_the_real_app_guards_web_and_admin_routes():
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=real_app), base_url="http://testserver"
    ) as http:
        assert (await http.get(f"{WEB}/materials")).status_code == 401
        assert (await http.get(f"{ADMIN}/usage")).status_code == 401
    paths = {getattr(r, "path", "") for r in real_app.routes}
    assert (
        f"{WEB}/recognitions" in paths
        and f"{ADMIN}/materials/{{material_id}}/purge" in paths
    )
    assert f"{INTERNAL}/service/market-offers/check" in paths
