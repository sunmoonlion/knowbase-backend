"""资料库接口 `user-data/v2`（SDD 0043 块 1a）：走一遍 HTTP，库是真的。"""

from __future__ import annotations

import hashlib
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI
from test_durable_delivery_db import db as db
from test_user_data import CSV, Clock, MemoryObjects

from app.application.services.call_rate import RateLimiter
from app.application.services.user_library_service import UserLibraryService
from app.application.services.user_resolution_service import UserResolutionService
from app.application.services.user_run_service import UserRunService
from app.domain import chart_of_accounts
from app.domain.user_data import Owner
from app.infrastructure.repositories.user_library import SqlUserLibraryRepository
from app.infrastructure.repositories.user_resolution import (
    SqlUserResolutionRepository,
)
from app.infrastructure.repositories.user_runs import SqlUserRunRepository
from app.interfaces.endpoints import (
    user_fact_routes,
    user_library_routes,
    user_resolution_routes,
    user_run_routes,
)
from app.interfaces.errors.exception_handlers import register_exception_handlers

BASE = "/api/internal/v2/user-data"


@pytest.fixture
def api(db):
    objects, clock = MemoryObjects(), Clock()
    service = UserLibraryService(
        SqlUserLibraryRepository(lambda: db), objects, prefix="p", clock=clock
    )
    runs = UserRunService(
        SqlUserLibraryRepository(lambda: db),
        SqlUserRunRepository(lambda: db),
        objects,
        prefix="p",
        clock=clock,
    )
    resolver = UserResolutionService(
        SqlUserResolutionRepository(lambda: db),
        SqlUserLibraryRepository(lambda: db),
        SqlUserRunRepository(lambda: db),
        clock=clock,
        limiter=RateLimiter(1000),
    )
    app = FastAPI()
    register_exception_handlers(app)
    app.include_router(user_library_routes.router, prefix="/api")
    app.include_router(user_run_routes.router, prefix="/api")
    app.include_router(user_resolution_routes.router, prefix="/api")
    app.include_router(user_fact_routes.router, prefix="/api")
    state = {"owner": Owner("alice", "knowbench")}
    overrides = app.dependency_overrides
    overrides[user_library_routes.internal_host_only] = lambda: None
    overrides[user_library_routes.require_user_owner] = lambda: state["owner"]
    overrides[user_library_routes.require_subscribing_owner] = lambda: state["owner"]
    overrides[user_library_routes.get_user_library_service] = lambda: service
    overrides[user_run_routes.get_user_run_service] = lambda: runs
    overrides[user_resolution_routes.get_user_resolution_service] = lambda: resolver
    state["service"] = "knowlab"
    caller = lambda: SimpleNamespace(caller=state["service"])  # noqa: E731
    overrides[user_library_routes.require_directory_service] = caller
    overrides[user_library_routes.require_purchase_service] = caller
    for need in (
        user_run_routes.require_grant_test_service,
        user_run_routes.require_output_service,
        user_run_routes.require_supply_service,
        user_run_routes.require_read_service,
    ):
        overrides[need] = lambda: SimpleNamespace(
            caller=state["service"], principal=SimpleNamespace(subject="svc-1")
        )
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
            "doc_type": "statistical_data",
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
    # 上传时的识别也留痕：记规则版本（0049 第六节）
    assert [h["what"] for h in history.json()] == [
        "recognized",
        "description",
        "license",
    ]
    assert history.json()[0]["after"]["rules_version"] == 1

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
    assert vocab["version"] == 2
    assert vocab["doc_types"]["annual_report"] == {
        "name": "年报",
        "class": "disclosure",
    }
    assert vocab["doc_classes"]["disclosure"]["publisher"] == "上市公司及信披义务人"
    assert vocab["topics"]["dividend"] == "分红"
    assert len(vocab["industries"]) == 29
    assert "cn_security" in vocab["subject_kinds"]
    assert "interim_announcement" in vocab["collectable"]["security"]


async def test_bad_descriptions_are_named(api):
    client, _ = api
    refused = await client.post(
        f"{BASE}/materials", params={"name": "a.csv", "subject": "茅台"}, content=CSV
    )
    assert (refused.status_code, refused.json()["code"]) == (422, "subject_invalid")


async def test_research_reports_carry_their_dimensions(api):
    """SDD 0049：研报的几维照 Wind 研报平台；只有研报能带；描述读出带大类。"""
    client, _ = api
    material_id = await _upload(client, "中信-茅台点评.csv")
    described = await client.put(
        f"{BASE}/materials/{material_id}/description",
        json={
            "subject": "600519.SH",
            "doc_type": "research_company",
            "industry": "食品饮料",
            "topics": ["results"],
            "language": "zh",
            "attributes": {
                "institution": "中信证券",
                "analysts": ["张三"],
                "rating": "买入",
                "forecast": ["rating_change"],
                "depth": "regular",
            },
            "confirm": True,
        },
    )
    assert described.status_code == 200, described.text
    d = described.json()["material"]["description"]
    assert (d["doc_type"], d["doc_class"], d["industry"]) == (
        "research_company",
        "research",
        "食品饮料",
    )
    assert d["attributes"]["institution"] == "中信证券"
    for bad, code in (
        ({"doc_type": "research"}, "doc_type_invalid"),  # 描述只记细类
        (
            {"doc_type": "annual_report", "attributes": {"rating": "买入"}},
            "attributes_invalid",
        ),
        ({"doc_type": "research_company", "industry": "白酒"}, "industry_invalid"),
        ({"doc_type": "research_company", "topics": ["gossip"]}, "topics_invalid"),
    ):
        refused = await client.put(
            f"{BASE}/materials/{material_id}/description", json=bad
        )
        assert (refused.status_code, refused.json()["code"]) == (422, code), bad


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
    # 交付时冻结：作者绑给 e1 的每份钉在当时那一版
    bound = await _upload(client, "绑定.csv")
    await client.post(f"{BASE}/materials/{bound}/uses", json={"use": "expert:e1"})
    frozen = await client.post(
        f"{BASE}/experts/e1/versions/1.0/freeze", json={"author": "bob"}
    )
    assert frozen.status_code == 201, frozen.text
    assert [(u["use"], u["material_id"], u["version_no"]) for u in frozen.json()] == [
        ("expert:e1@1.0", bound, 1)
    ]
    state["owner"] = Owner("alice", "knowbench")
    assert (await client.get(f"{BASE}/experts")).json() == []


async def test_platform_subscription_and_processing(api):
    """块 1b 余下：平台过程按次、订阅只经 knowfeed、上传时采用加工。"""
    client, state = api
    state["owner"] = Owner("alice", "knowlab")
    material_id = await _upload(client)
    made = await client.post(
        f"{BASE}/platform-consents",
        json={
            "kind": "incubate",
            "ref": "i-1",
            "items": [{"material_id": material_id}],
        },
    )
    assert made.status_code == 201, made.text
    assert made.json()[0]["use"] == "platform:knowlab:incubate:i-1"
    assert made.json()[0]["material_id"] == material_id
    listed = await client.get(f"{BASE}/platform-consents/incubate/i-1")
    assert len(listed.json()) == 1
    gone = await client.delete(f"{BASE}/platform-consents/incubate/i-1")
    assert gone.status_code == 204
    assert (await client.get(f"{BASE}/platform-consents/incubate/i-1")).json() == []
    state["owner"] = Owner("alice", "knowbench")
    wrong = await client.post(
        f"{BASE}/platform-consents",
        json={"kind": "test", "ref": "t-1", "items": [{"material_id": material_id}]},
    )
    assert (wrong.status_code, wrong.json()["code"]) == (422, "platform_app_invalid")

    scope = {
        "by": "url",
        "starts_on": "2026-10-27",
        "ends_on": "2027-04-27",
        "purpose": "看央行公告",
        "urls": ["https://www.pbc.gov.cn/rss"],
    }
    denied = await client.put(f"{BASE}/subscriptions/s-1/revisions/1", json=scope)
    assert (denied.status_code, denied.json()["code"]) == (
        403,
        "subscription_app_denied",
    )
    state["owner"] = Owner("alice", "knowfeed")
    made = await client.put(f"{BASE}/subscriptions/s-1/revisions/1", json=scope)
    assert made.status_code == 200, made.text
    assert made.json()["urls"] == scope["urls"] and made.json()["revision"] == 1
    clash = await client.put(
        f"{BASE}/subscriptions/s-1/revisions/1", json={**scope, "purpose": "别的"}
    )
    assert (clash.status_code, clash.json()["code"]) == (
        409,
        "subscription_revision_exists",
    )
    listed = (await client.get(f"{BASE}/subscriptions")).json()
    assert [(s["subscription_id"], s["revision"]) for s in listed] == [("s-1", 1)]
    gone = await client.delete(f"{BASE}/subscriptions/s-1/revisions/1")
    assert gone.status_code == 204
    assert (await client.get(f"{BASE}/subscriptions")).json() == []

    created = await client.post(
        f"{BASE}/materials", params={"name": "扫描.csv", "process": "true"}, content=CSV
    )
    processed = created.json()["material"]["material_id"]
    uses = (await client.get(f"{BASE}/materials/{processed}/uses")).json()
    assert [u["use"].split(":")[0] for u in uses] == ["processing"]
    later = await client.post(f"{BASE}/materials/{material_id}/versions/1/processing")
    assert later.status_code == 201 and later.json()["version_no"] == 1


async def test_run_grants_reads_outputs_and_processing(api):
    """块 1c：本人签委托、执行方凭授权读与交还；产出区写入与转为数据；加工写回读法。"""
    client, state = api
    created = await client.post(
        f"{BASE}/materials", params={"name": "in.csv", "process": "true"}, content=CSV
    )
    body = created.json()
    material_id, sha = body["material"]["material_id"], body["version"]["sha256"]
    await client.post(
        f"{BASE}/materials/{material_id}/uses", json={"use": "app:knowbench"}
    )
    ask = {
        "material_id": material_id,
        "version_no": 1,
        "sha256": sha,
        "basis": "display",
        "basis_ref": "app:knowbench",
    }
    grant = await client.post(
        f"{BASE}/run-grants",
        json={"process": "delegation", "ref": "task-1", "items": [ask]},
    )
    assert grant.status_code == 201, grant.text
    grant_id = grant.json()["grant_id"]
    assert grant.json()["items"][0]["content_owner"] == "alice"

    state["service"] = "knowbench"
    view = await client.get(f"{BASE}/run-grants/{grant_id}")
    assert view.json()["pins"][0]["sha256"] == sha
    content = await client.get(
        f"{BASE}/run-grants/{grant_id}/materials/{material_id}/content"
    )
    assert content.content == CSV and content.headers["X-Content-SHA256"] == sha
    state["service"] = "knowmart"
    denied = await client.get(f"{BASE}/run-grants/{grant_id}")
    assert (denied.status_code, denied.json()["code"]) == (403, "service_scope_denied")
    state["service"] = "knowbench"
    assert (await client.post(f"{BASE}/runs/task-1/release")).status_code == 204
    gone = await client.get(
        f"{BASE}/run-grants/{grant_id}/materials/{material_id}/content"
    )
    assert (gone.status_code, gone.json()["code"]) == (403, "grant_inactive")
    reads = (await client.get(f"{BASE}/materials/{material_id}/reads")).json()
    assert [(r["operation"], r["beneficiary"]) for r in reads] == [("content", "alice")]

    written = await client.post(
        f"{BASE}/outputs",
        params={
            "beneficiary": "alice",
            "task_ref": "task-1",
            "run_ref": "run-1",
            "name": "report.md",
            "input_set_id": grant.json()["input_set_id"],
        },
        content=b"# done\n",
    )
    assert written.status_code == 201, written.text
    output_id = written.json()["material_id"]
    converted = await client.post(f"{BASE}/materials/{output_id}/convert", json={})
    assert converted.status_code == 201, converted.text
    assert converted.json()["category"] == "uploaded"
    assert converted.json()["material"]["provenance"]["converted_from"] == output_id

    state["service"] = "knowfeed"
    pending = (await client.get(f"{BASE}/processing/pending")).json()
    assert pending == [{"owner": "alice", "material_id": material_id, "version_no": 1}]
    processing = await client.post(
        f"{BASE}/processing/grants",
        json={"owner": "alice", "material_id": material_id, "version_no": 1},
    )
    processing_id = processing.json()["grant_id"]
    text = await client.post(
        f"{BASE}/processing/grants/{processing_id}/renditions/text",
        params={
            "processor_version": "0.1",
            "title": "2025 年年度报告",
            "published_on": "2026-03-28",
        },
        content="正文".encode(),
    )
    assert text.status_code == 201 and text.json()["status"] == "ready"
    pages = await client.post(
        f"{BASE}/processing/grants/{processing_id}/renditions/pages",
        params={"processor_version": "0.1"},
        content=b'[{"page": 2, "text": "b"}, {"page": 1, "text": "a"}]',
    )
    assert pages.status_code == 201 and pages.json()["detail"] == {"pages": 2}
    bad = await client.post(
        f"{BASE}/processing/grants/{processing_id}/renditions/pages",
        params={"processor_version": "0.1"},
        content=b'{"page": 1}',
    )
    assert (bad.status_code, bad.json()["code"]) == (422, "pages_invalid")
    failed = await client.post(
        f"{BASE}/processing/grants/{processing_id}/renditions/table",
        params={"processor_version": "0.1", "failed_reason": "no tables"},
    )
    assert failed.json()["detail"] == {"reason": "no tables"}
    listed = await client.get(f"{BASE}/materials/{material_id}/versions/1/renditions")
    assert [(r["kind"], r["status"]) for r in listed.json()] == [
        ("pages", "ready"),
        ("table", "failed"),
        ("text", "ready"),
    ]

    # 钉住按页文字读：回的就是写回时那个格式，页按页码排
    state["owner"] = Owner("alice", "knowbench")
    pinned = await client.post(
        f"{BASE}/run-grants",
        json={
            "process": "delegation",
            "ref": "task-p",
            "items": [
                {
                    "material_id": material_id,
                    "version_no": 1,
                    "sha256": sha,
                    "basis": "display",
                    "basis_ref": "app:knowbench",
                    "rendition_kind": "pages",
                    "rendition_no": 1,
                    "rendition_sha256": pages.json()["sha256"],
                }
            ],
        },
    )
    assert pinned.status_code == 201, pinned.text
    state["service"] = "knowbench"
    got = await client.get(
        f"{BASE}/run-grants/{pinned.json()['grant_id']}"
        f"/materials/{material_id}/rendition"
    )
    assert got.status_code == 200, got.text
    assert got.headers["X-Rendition"] == "pages/1"
    assert got.headers["X-Content-SHA256"] == pages.json()["sha256"]
    assert got.json() == [{"page": 1, "text": "a"}, {"page": 2, "text": "b"}]


async def test_knowlab_platform_grants_end_with_the_process(api):
    client, state = api
    state["owner"] = Owner("alice", "knowlab")
    material_id = await _upload(client)
    entry = (await client.get(f"{BASE}/materials/{material_id}")).json()
    await client.post(
        f"{BASE}/platform-consents",
        json={"kind": "test", "ref": "t-9", "items": [{"material_id": material_id}]},
    )
    state["service"] = "knowlab"
    machine = await client.post(
        f"{BASE}/service/run-grants",
        json={
            "process": "platform",
            "beneficiary": "alice",
            "ref": "t-9/machine",
            "reader_role": "machine",
            "items": [
                {
                    "material_id": material_id,
                    "version_no": 1,
                    "sha256": entry["version"]["sha256"],
                    "basis": "platform",
                    "basis_ref": "platform:knowlab:test:t-9",
                }
            ],
        },
    )
    assert machine.status_code == 201, machine.text
    grant_id = machine.json()["grant_id"]
    ended = await client.post(
        f"{BASE}/service/platform-processes/test/t-9/end",
        json={"subject": "alice", "reason": "finished"},
    )
    assert ended.status_code == 204
    state["service"] = "knowbench"  # 机器由运行方 knowbench 读
    read = await client.get(
        f"{BASE}/run-grants/{grant_id}/materials/{material_id}/content"
    )
    assert (read.status_code, read.json()["code"]) == (403, "grant_inactive")
    assert (await client.get(f"{BASE}/platform-consents/test/t-9")).json() == []


async def test_resolve_confirm_and_knowfeed_checks(api):
    """块 2：按描述查、解析、确认、knowfeed 核确认、偏好只读。"""
    client, state = api
    await client.put(f"{BASE}/account", json={"default_display": True})
    created = await client.post(
        f"{BASE}/materials",
        params={
            "name": "maotai-2025.csv",
            "subject": "600519.SH",
            "doc_type": "annual_report",
            "period_start": "2025-01-01",
            "period_end": "2025-12-31",
            "disclosed_on": "2026-03-28",
            "confirm": "true",
        },
        content=CSV,
    )
    material_id = created.json()["material"]["material_id"]
    found = await client.get(
        f"{BASE}/lookup",
        params={
            "subject": "600519.SH",
            "doc_type": "annual_report",
            "period_start": "2025-01-01",
            "period_end": "2025-12-31",
        },
    )
    assert found.json() == {"status": "shown", "zone": "own"}

    need = {
        "key": "annual",
        "name": "年报",
        "subject": "600519.SH",
        "doc_type": "annual_report",
        "period": {"kind": "latest", "count": 2},
        "formats": ["csv", "pdf"],
    }
    resolved = await client.post(
        f"{BASE}/resolutions",
        json={
            "consumer": "expert:fa-1@2",
            "contract_version": "data-request/v1",
            "needs": [need],
            "params": {"task_date": "2026-10-27"},
        },
    )
    assert resolved.status_code == 201, resolved.text
    body = resolved.json()
    assert [(i["key"]["period_end"], i["status"]) for i in body["items"]] == [
        ("2025-12-31", "resolved"),
        ("2024-12-31", "to_confirm"),
    ]
    assert body["items"][0]["chosen"][0]["material_id"] == material_id
    assert body["items"][0]["chosen"][0]["name"] == "maotai-2025.csv"
    assert body["items"][0]["chosen"][0]["size_bytes"] == len(CSV)
    again = await client.get(f"{BASE}/resolutions/{body['resolution_id']}")
    assert again.json()["revision"] == 1

    confirmed = await client.post(
        f"{BASE}/confirmations",
        json={
            "kind": "description",
            "resolution_id": body["resolution_id"],
            "items": [{"key": body["items"][1]["key"], "need_key": "annual"}],
            "supply": {"prematch": "ok"},
        },
    )
    assert confirmed.status_code == 201, confirmed.text
    confirmation_id = confirmed.json()["confirmation_id"]
    assert confirmed.json()["branches"] == [{"item_id": "c1", "branch": "supply"}]

    state["service"] = "knowfeed"
    checked = await client.get(
        f"{BASE}/service/confirmations/{confirmation_id}", params={"subject": "alice"}
    )
    assert checked.status_code == 200, checked.text
    assert checked.json()["supply"] == {"prematch": "ok"}
    assert checked.json()["needs"][0]["key"] == "annual"
    prefs = await client.get(
        f"{BASE}/preferences",
        params={"consumer": "expert:fa-1@2"},
    )
    assert prefs.json() == []

    # 等数据的委托：knowbench 以服务身份看清单、重解析、凭确认续签（所有者 10-27 定）
    state["service"] = "knowbench"
    rid = body["resolution_id"]
    seen = await client.get(
        f"{BASE}/service/resolutions/{rid}", params={"subject": "alice"}
    )
    assert seen.json()["revision"] == 1
    refreshed = await client.post(
        f"{BASE}/service/resolutions/{rid}/refresh", params={"subject": "alice"}
    )
    assert refreshed.status_code == 200, refreshed.text
    assert [i["status"] for i in refreshed.json()["items"]] == ["resolved", "waiting"]
    chosen = refreshed.json()["items"][0]["chosen"][0]
    pin = {
        "material_id": chosen["material_id"],
        "version_no": chosen["version_no"],
        "sha256": chosen["sha256"],
        "basis": "display",
        "basis_ref": "app:knowbench",
    }
    grant = await client.post(
        f"{BASE}/service/run-grants/by-confirmation",
        json={
            "subject": "alice",
            "confirmation_id": confirmation_id,
            "resolution_id": rid,
            "revision": refreshed.json()["revision"],
            "ref": "knowbench:k-1",
            "items": [pin],
        },
    )
    # 这张确认没带任务：对不上
    assert grant.json()["code"] == "confirmation_mismatch"
    state["service"] = "knowfeed"
    denied = await client.post(
        f"{BASE}/service/resolutions/{rid}/refresh", params={"subject": "alice"}
    )
    assert denied.json()["code"] == "service_scope_denied"

    # 用户确认前改了清单（换主体）：整份换掉重新解析，出新修订（decisions ⑭）
    revised = await client.put(
        f"{BASE}/resolutions/{rid}/needs",
        json={"needs": [{**need, "subject": "000858.SZ"}]},
    )
    assert revised.status_code == 201, revised.text
    assert revised.json()["revision"] == refreshed.json()["revision"] + 1
    assert {i["key"]["subject"] for i in revised.json()["items"]} == {"000858.SZ"}


async def test_knowfeed_delivers_public_originals_and_references(api):
    """2b：knowfeed 交公开原件（只存一份）、给提了需求的用户记引用。"""
    client, state = api
    params = {
        "channel": "cninfo",
        "channel_doc_id": "1219876543",
        "name": "贵州茅台2025年年度报告.csv",
        "subject": "600519.SH",
        "doc_type": "annual_report",
        "period_start": "2025-01-01",
        "period_end": "2025-12-31",
        "disclosed_on": "2026-03-28",
    }
    state["service"] = "knowbench"
    denied = await client.post(
        f"{BASE}/service/public-originals", params=params, content=CSV
    )
    assert (denied.status_code, denied.json()["code"]) == (403, "service_scope_denied")
    state["service"] = "knowfeed"
    made = await client.post(
        f"{BASE}/service/public-originals", params=params, content=CSV
    )
    assert made.status_code == 201, made.text
    assert made.json()["created"] is True and made.json()["version_no"] == 1
    again = await client.post(
        f"{BASE}/service/public-originals", params=params, content=CSV
    )
    assert again.json()["created"] is False
    assert again.json()["material_id"] == made.json()["material_id"]
    refs = await client.post(
        f"{BASE}/service/public-originals/{made.json()['material_id']}/references",
        json={"version_no": 1, "references": [{"subject": "alice", "ref": "basis-1"}]},
    )
    assert refs.json() == {"recorded": 1}
    shelf = (
        await client.get(f"{BASE}/materials", params={"category": "collected"})
    ).json()
    assert [e["material"]["material_id"] for e in shelf] == [made.json()["material_id"]]
    assert shelf[0]["material"]["content_origin"] == "disclosure"


async def test_facts_written_by_knowfeed_and_queried_by_run_and_market(api):
    """2c：knowfeed 写回抽好的表；执行的应用按运行查；本人跨全市场排名；科目表。"""
    client, state = api
    params = {
        "channel": "cninfo",
        "channel_doc_id": "ar-600519-2025",
        "name": "maotai-2025.csv",
        "subject": "600519.SH",
        "doc_type": "annual_report",
        "period_start": "2025-01-01",
        "period_end": "2025-12-31",
    }
    state["service"] = "knowfeed"
    made = (
        await client.post(
            f"{BASE}/service/public-originals", params=params, content=CSV
        )
    ).json()
    grant = await client.post(
        f"{BASE}/processing/grants",
        json={
            "owner": "public:disclosure",
            "material_id": made["material_id"],
            "version_no": 1,
        },
    )
    assert grant.status_code == 201, grant.text
    written = await client.post(
        f"{BASE}/processing/grants/{grant.json()['grant_id']}/facts",
        json={
            "processor_version": "ar-0.1",
            "raw": [
                {
                    "label": "营业收入",
                    "value": "1500",
                    "unit": "万元",
                    "period_end": "2025-12-31",
                    "duration": "year",
                    "consolidation": "consolidated",
                    "page": 88,
                }
            ],
            "standard": [
                {
                    "code": "IS.REVENUE",
                    "value": "15000000",
                    "period_end": "2025-12-31",
                    "duration": "year",
                    "consolidation": "consolidated",
                    "derived_from": [0],
                    "rule": "chart-v1:alias",
                    "page": 88,
                }
            ],
        },
    )
    assert written.status_code == 201, written.text
    assert written.json()["kind"] == "table"

    market = await client.post(f"{BASE}/market-facts", json={"codes": ["IS.REVENUE"]})
    assert market.status_code == 200, market.text
    assert [(r["subject"], r["page"]) for r in market.json()] == [("600519.SH", 88)]
    chart = (await client.get(f"{BASE}/chart")).json()
    assert chart["version"] == chart_of_accounts.CHART_VERSION and "gross_margin" in {
        m["key"] for m in chart["metrics"]
    }

    # 公开原件不在他的总栏里，经授权或查事实读
    hidden = await client.get(f"{BASE}/materials/{made['material_id']}")
    assert hidden.status_code == 404
    signed = await client.post(
        f"{BASE}/run-grants",
        json={
            "process": "delegation",
            "ref": "task-facts",
            "items": [
                {
                    "material_id": made["material_id"],
                    "version_no": 1,
                    "sha256": made["sha256"],
                    "basis": "public",
                    "basis_ref": "public:disclosure",
                }
            ],
        },
    )
    assert signed.status_code == 201, signed.text
    state["service"] = "knowbench"
    run = await client.post(
        f"{BASE}/run-facts",
        json={"run_ref": "task-facts", "beneficiary": "alice", "codes": ["IS.REVENUE"]},
    )
    assert run.status_code == 200, run.text
    assert [(r["value"], r["rendition_no"]) for r in run.json()] == [
        ("15000000.000000", 1)
    ]
    # 读数留痕只给所有者看；公开原件不归任何用户
    reads = await client.get(f"{BASE}/materials/{made['material_id']}/reads")
    assert (reads.status_code, reads.json()["code"]) == (404, "item_not_found")
