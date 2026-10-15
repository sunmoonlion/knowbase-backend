"""数据目录的页面接口（PRD/apps/knowledge.md，`AT-KNOW-01` 至 `08`）。

数据集文件用的是 `test_knowledge_datasets` 里那一份实采夹具。不访问外网，不需要数据库
（登记表的全部版本另在 `test_dataset_registry_db` 里用真数据库测）。
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import replace
from datetime import UTC, datetime

import httpx
import pytest
from test_auth_routes_security import FakeAuthService, session
from test_knowledge_datasets import (
    BUCKET,
    FIXTURE,
    KEY,
    TOKEN,
    VERSION,
    LocalFiles,
    MemoryRegistry,
    catalog_for,
    registered,
    registration,
    settings_for,
)
from test_knowledge_datasets import default_dataset as default_dataset

import app.interfaces.http.middleware.auth as auth_middleware
from app.application.services.dataset_catalog import UnknownDataset
from app.application.services.dataset_pages import DatasetPages
from app.application.services.dataset_registry_view import DatasetRegistryView
from app.domain.dataset_view import matches, metric_view, note_label, notes_of
from app.domain.datasets import SUPERSEDED
from app.interfaces.http.admin.catalog import get_dataset_pages
from app.interfaces.http.admin.datasets import get_registry_view, registry_settings
from app.interfaces.mcp.knowbase_mcp import KnowledgeMcp
from app.main import app

NEWER = "sh600009-financials-ffffffffffffffff"
DATASET = "sh600009-financials"


# ---------------- 整理的规则 ----------------
@pytest.mark.parametrize(
    ("query", "found"),
    [
        (None, True),
        ("", True),
        ("  ", True),
        ("600009", True),
        ("6000", True),
        ("上海机场", True),
        ("机场", True),
        ("SH600009", True),  # 不分大小写，数据集标识也算
        ("600519", False),
        ("茅台", False),
    ],
)
def test_search_is_by_code_name_or_dataset(query, found):
    entry = {
        "dataset": DATASET,
        "title": "上海机场 财务报表",
        "security_code": "600009",
    }
    assert matches(entry, query) is found


def test_search_does_not_trip_over_a_dataset_without_a_security():
    entry = {"dataset": "retail", "title": "零售经营库", "security_code": None}
    assert matches(entry, "零售") and not matches(entry, "600009")


def test_notes_are_split_into_sources_and_limitations():
    sources, limitations = notes_of(
        {
            "data_snapshot_id": "v1",
            "start_date": "2020-01-01",
            "security_code": "600009",
            "statement_source": "第三方整理",
            "verified_range": "2017 至 2025",
            "interim_note": "中报未核实",
            "restatement_explanation_2021": "同一控制下企业合并",
            "something_new": "新的说明",
            "empty_note": "",
        }
    )
    assert [n["key"] for n in sources] == ["statement_source", "verified_range"]
    assert [(n["key"], n["label"]) for n in limitations] == [
        ("interim_note", "中报与季报"),
        ("restatement_explanation_2021", "2021 年追溯调整的原因"),
        ("something_new", "其他说明"),  # 不认识的说明照样列出来，不丢
    ]
    assert note_label("license_note") == "使用范围"


def test_a_metric_from_an_older_dataset_has_no_made_up_fields():
    old = metric_view(
        {
            "metric_name": "net_revenue_cents",
            "display_name": "净营收",
            "source_table": "order_performance",
            "expression_hint": "SUM(net_revenue_cents)",
            "unit": "分",
            "time_basis": "下单日",
            "description": "扣退款后",
        }
    )
    assert old["tables"] == ["order_performance"]
    assert old["expression"] == "SUM(net_revenue_cents)"
    assert old["queryable"] is None and old["applicable_when"] is None
    new = metric_view(
        {
            "metric_name": "gross_margin",
            "source_table": "income_statement",
            "base_table": "income_statement",
            "expression_hint": "x",
            "value_expression": "(a - b) / a",
            "queryable": "1",
        }
    )
    assert new["tables"] == ["income_statement"]
    assert new["expression"] == "(a - b) / a" and new["queryable"] is True
    assert metric_view({"metric_name": "m", "queryable": 0})["queryable"] is False
    # 用到几张表的口径：拆开列，不重复
    across = metric_view(
        {
            "metric_name": "ocf_to_netprofit",
            "base_table": "cash_flow",
            "source_table": "cash_flow+income_statement",
        }
    )
    assert across["tables"] == ["cash_flow", "income_statement"]


# ---------------- 页面背后的读法 ----------------
def pages_for(default_dataset, tmp_path, registry=None, **changes):
    settings = settings_for(default_dataset, tmp_path, **changes)
    registry = registry if registry is not None else MemoryRegistry(registered())
    catalog = catalog_for(settings, registry, LocalFiles(tmp_path))
    enabled = settings.knowbase_dataset_registry_enabled
    return DatasetPages(catalog, registry_enabled=enabled), catalog, settings


async def test_the_page_lists_what_the_tool_lists(default_dataset, tmp_path):
    """`AT-KNOW-01`、`F-KNOW-11`：页面与工具列的是同一份。"""
    registry = MemoryRegistry()
    pages, catalog, settings = pages_for(default_dataset, tmp_path, registry)
    server = KnowledgeMcp(settings, catalog)

    async def tool() -> list[dict]:
        reply = await server.handle(
            server.tokens.resolve(f"Bearer {TOKEN}"),
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {"name": "list_datasets", "arguments": {}},
            },
        )
        return reply["result"]["structuredContent"]["datasets"]

    assert [d["dataset"] for d in (await pages.listing())["datasets"]] == ["retail"]
    registry.rows[DATASET] = registered()
    catalog._loaded_at = None  # 目录隔一会儿才重读登记表；这里不等
    page = (await pages.listing())["datasets"]
    assert [d["dataset"] for d in page] == ["retail", DATASET]
    listed = await tool()
    assert [{k: v for k, v in d.items() if k != "updated_at"} for d in page] == listed
    assert page[1]["data_version"] == listed[1]["data_version"] == VERSION
    assert page[1]["updated_at"] == datetime(2026, 9, 27, tzinfo=UTC)
    assert page[0]["updated_at"] is None  # 默认数据集不经登记
    assert all("updated_at" not in d for d in listed)  # 工具的返回没有变


async def test_a_newer_version_replaces_the_one_on_the_page(default_dataset, tmp_path):
    """`AT-KNOW-02` 的前一半：目录里显示新版本。"""
    registry = MemoryRegistry(registered())
    pages, catalog, _ = pages_for(default_dataset, tmp_path, registry)
    assert (await pages.detail(DATASET))["data_version"] == VERSION
    registry.rows[DATASET] = registered(registration(data_version=NEWER))
    catalog._loaded_at = None
    assert (await pages.listing())["datasets"][1]["data_version"] == NEWER
    assert (await pages.detail(DATASET))["data_version"] == NEWER
    assert list(pages._details) == [(DATASET, NEWER)]  # 旧版本的结构不留着


async def test_searching_keeps_the_total(default_dataset, tmp_path):
    """`AT-KNOW-03`：搜一家没有数据的公司，显示没有。"""
    pages, _, _ = pages_for(default_dataset, tmp_path)
    found = await pages.listing("600009")
    assert [d["dataset"] for d in found["datasets"]] == [DATASET]
    missing = await pages.listing("600519")
    assert missing["datasets"] == [] and missing["total"] == 2
    assert missing["registry_enabled"] is True


async def test_a_dataset_shows_structure_and_no_data(default_dataset, tmp_path):
    """`AT-KNOW-04`、`AT-KNOW-05`：表、列、口径、局限都有；没有数值，没有存放位置。"""
    pages, _, _ = pages_for(default_dataset, tmp_path)
    detail = await pages.detail(DATASET)
    assert (detail["title"], detail["security_code"]) == ("上海机场 财务报表", "600009")
    assert (detail["start_date"], detail["end_date"]) == ("1994-12-31", "2026-06-30")

    tables = {t["name"]: t for t in detail["tables"]}
    assert tables["balance_sheet"]["kind"] == "data"
    assert tables["field_dictionary"]["kind"] == "dictionary"
    assert tables["balance_sheet"]["row_count"] > 0
    columns = {c["name"]: c for c in tables["balance_sheet"]["columns"]}
    assert columns["total_assets"]["label"] == "资产总计"
    assert columns["total_assets"]["unit"] == "元"
    assert columns["total_assets"]["type"]

    metrics = {m["name"]: m for m in detail["metrics"]}
    assert metrics["gross_margin"]["label"] == "毛利率"
    assert metrics["gross_margin"]["tables"] == ["income_statement"]
    assert "operate_income" in metrics["gross_margin"]["expression"]

    assert {n["key"] for n in detail["sources"]} == {
        "statement_source",
        "official_source",
        "verified_range",
    }
    limits = {n["key"]: n for n in detail["limitations"]}
    assert "不可直接比较" in limits["restatement_note"]["text"]
    assert limits["license_note"]["label"] == "使用范围"
    assert "data_snapshot_id" not in limits and "security_code" not in limits

    text = json.dumps(detail, ensure_ascii=False, default=str)
    assert BUCKET not in text and KEY not in text and "object_key" not in text
    with sqlite3.connect(FIXTURE) as c:
        values = [
            str(v)
            for row in c.execute(
                "SELECT total_assets, total_liabilities FROM balance_sheet "
                "WHERE total_assets IS NOT NULL LIMIT 20"
            )
            for v in row
            if v is not None
        ]
    assert values and not [v for v in values if v in text]
    assert "rows" not in detail and all("rows" not in t for t in detail["tables"])


async def test_the_default_dataset_has_a_page_too(default_dataset, tmp_path):
    """`AT-KNOW-08`：多数据集没打开，目录里只有默认数据集，详情也打得开。"""
    # 关着的时候，装配层不给目录登记表；这里按那个样子建
    settings = settings_for(
        default_dataset, tmp_path, knowbase_dataset_registry_enabled=False
    )
    pages = DatasetPages(catalog_for(settings, None, None), registry_enabled=False)
    listing = await pages.listing()
    assert listing["registry_enabled"] is False
    assert [d["dataset"] for d in listing["datasets"]] == ["retail"]
    detail = await pages.detail("retail")
    assert [t["name"] for t in detail["tables"] if t["kind"] == "data"] == [
        "order_performance"
    ]
    assert detail["metrics"][0]["label"] == "净营收"
    assert detail["limitations"] == [] and detail["sources"] == []
    # 这个老数据集没有列说明表：列照样列出来，只是没有中文名
    assert detail["tables"][-1]["columns"][0]["label"] is None
    with pytest.raises(UnknownDataset):
        await pages.detail(DATASET)


# ---------------- 管理端的目录接口（账 56：数据目录只在管理端） ----------------
def console(who: str | None = "owner") -> httpx.AsyncClient:
    http = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    )
    if who:
        http.cookies.set("sunmoonai_knowbase_admin_sid", who)
    return http


def admins() -> FakeAuthService:
    return FakeAuthService(
        {
            "owner": session("admin", "knowbase:admin"),
            "visitor": session("admin", "profile:read"),
        }
    )


@pytest.fixture
def catalog(default_dataset, tmp_path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(auth_middleware, "admin_auth_service", admins())
    pages, _, _ = pages_for(default_dataset, tmp_path)
    app.dependency_overrides[get_dataset_pages] = lambda: pages
    try:
        yield pages
    finally:
        app.dependency_overrides.pop(get_dataset_pages, None)


LIST = "/api/admin/v1/knowledge/catalog/datasets"


async def test_only_the_knowledge_admin_sees_the_catalog(catalog):
    """没登录的 401；登录了但不是 knowbase 管理员的 403。"""
    async with console(None) as http:
        assert (await http.get(LIST)).status_code == 401
        assert (await http.get(f"{LIST}/{DATASET}")).status_code == 401
    async with console("visitor") as http:
        assert (await http.get(LIST)).status_code == 403
        assert (await http.get(f"{LIST}/{DATASET}")).status_code == 403


async def test_the_list_and_the_search(catalog):
    async with console() as http:
        everything = await http.get(LIST)
        found = await http.get(LIST, params={"q": "600009"})
        missing = await http.get(LIST, params={"q": "600519"})
        too_long = await http.get(LIST, params={"q": "x" * 81})
    assert everything.status_code == 200
    body = everything.json()
    assert [d["dataset"] for d in body["datasets"]] == ["retail", DATASET]
    assert body["datasets"][1] == {
        "dataset": DATASET,
        "title": "上海机场 财务报表",
        "security_code": "600009",
        "data_version": VERSION,
        "start_date": "1994-12-31",
        "end_date": "2026-06-30",
        "default": False,
        "updated_at": "2026-09-27T00:00:00Z",
    }
    assert [d["dataset"] for d in found.json()["datasets"]] == [DATASET]
    assert missing.json() == {"registry_enabled": True, "total": 2, "datasets": []}
    assert too_long.status_code == 422
    for answer in (everything, found):
        assert BUCKET not in answer.text and "object_key" not in answer.text
        assert "sha256" not in answer.text


async def test_one_dataset_over_http(catalog):
    async with console() as http:
        got = await http.get(f"{LIST}/{DATASET}")
        unknown = await http.get(f"{LIST}/sh600519-financials")
    assert got.status_code == 200
    body = got.json()
    assert set(body) == {
        "dataset",
        "title",
        "security_code",
        "data_version",
        "start_date",
        "end_date",
        "default",
        "updated_at",
        "sources",
        "tables",
        "metrics",
        "limitations",
    }
    assert set(body["tables"][0]) == {"name", "kind", "row_count", "columns"}
    assert BUCKET not in got.text and "object_key" not in got.text
    assert unknown.status_code == 404


async def test_the_catalog_is_not_on_the_web_side():
    """账 56：用户侧没有数据目录。"""
    paths = {route.path for route in app.routes}
    assert not [p for p in paths if p.startswith("/api/web/v1/catalog")]


async def test_a_broken_registry_tells_the_page_nothing_internal(
    default_dataset, tmp_path, monkeypatch
):
    class Broken(MemoryRegistry):
        async def active(self):
            raise RuntimeError("connection to 10.0.0.5:5432 refused, password=hunter2")

    monkeypatch.setattr(auth_middleware, "admin_auth_service", admins())
    pages, _, _ = pages_for(default_dataset, tmp_path, Broken())
    app.dependency_overrides[get_dataset_pages] = lambda: pages
    try:
        async with console() as http:
            listing = await http.get(LIST)
            detail = await http.get(f"{LIST}/{DATASET}")
    finally:
        app.dependency_overrides.pop(get_dataset_pages, None)
    assert (listing.status_code, detail.status_code) == (503, 503)
    assert "hunter2" not in listing.text + detail.text
    assert "10.0.0.5" not in listing.text + detail.text


# ---------------- 管理端的接口 ----------------
class History:
    def __init__(self, *rows) -> None:
        self.rows = list(rows)

    async def versions(self, dataset_id: str | None = None):
        rows = sorted(
            self.rows, key=lambda r: (r.dataset_id, -r.registered_at.timestamp())
        )
        return [r for r in rows if dataset_id in (None, r.dataset_id)]


class Fetched:
    def __init__(self, *versions: str) -> None:
        self.versions = set(versions)

    def fetched(self, dataset) -> bool:
        return dataset.data_version in self.versions


def two_versions():
    old = replace(
        registered(),
        status=SUPERSEDED,
        changed_at=datetime(2026, 9, 30, 8, tzinfo=UTC),
    )
    new = replace(
        registered(registration(data_version=NEWER, sha256="f" * 64)),
        registered_at=datetime(2026, 9, 30, 8, tzinfo=UTC),
        changed_at=datetime(2026, 9, 30, 8, tzinfo=UTC),
    )
    return old, new


@pytest.fixture
def admin(default_dataset, tmp_path, monkeypatch: pytest.MonkeyPatch):
    fake = FakeAuthService(
        {
            "owner": session("admin", "knowbase:admin"),
            "visitor": session("admin", "profile:read"),
        }
    )
    monkeypatch.setattr(auth_middleware, "admin_auth_service", fake)
    state = {"settings": settings_for(default_dataset, tmp_path)}
    view = DatasetRegistryView(History(*two_versions()), Fetched(NEWER))
    app.dependency_overrides[registry_settings] = lambda: state["settings"]
    app.dependency_overrides[get_registry_view] = lambda: view
    try:
        yield state
    finally:
        app.dependency_overrides.pop(registry_settings, None)
        app.dependency_overrides.pop(get_registry_view, None)


REGISTRY = "/api/admin/v1/knowledge/datasets"


async def test_only_the_knowledge_admin_sees_the_registry(admin):
    """`AT-KNOW-07`：没登录、权限不够、拿网页端的登录来，都看不到。"""
    async with console(None) as http:
        assert (await http.get(REGISTRY)).status_code == 401
    async with console("visitor") as http:
        assert (await http.get(REGISTRY)).status_code == 403
        assert (await http.get(f"{REGISTRY}/{DATASET}/versions")).status_code == 403
    # 拿网页端的登录来：管理接口不认
    async with console(None) as http:
        http.cookies.set("sunmoonai_knowbase_web_sid", "owner")
        assert (await http.get(REGISTRY)).status_code == 401
        assert (await http.get(LIST)).status_code == 401


async def test_the_registry_shows_the_current_version(admin):
    async with console() as http:
        got = await http.get(REGISTRY)
    assert got.status_code == 200
    body = got.json()
    assert body["enabled"] is True and len(body["datasets"]) == 1
    row = body["datasets"][0]
    assert (row["dataset_id"], row["version_count"]) == (DATASET, 2)
    current = row["current"]
    assert (current["data_version"], current["status"]) == (NEWER, "active")
    assert current["sha256"] == "f" * 64 and current["size_bytes"] > 0
    assert (current["bucket"], current["object_key"]) == (BUCKET, KEY)
    assert current["fetched"] is True and current["superseded_at"] is None


async def test_the_history_marks_what_was_superseded_and_when(admin):
    """`AT-KNOW-02` 的后一半：历史里旧版本标为被取代。"""
    async with console() as http:
        got = await http.get(f"{REGISTRY}/{DATASET}/versions")
        unknown = await http.get(f"{REGISTRY}/sh600519-financials/versions")
    assert got.status_code == 200
    versions = got.json()["versions"]
    assert [(v["data_version"], v["status"]) for v in versions] == [
        (NEWER, "active"),
        (VERSION, "superseded"),
    ]
    assert versions[1]["superseded_at"] == "2026-09-30T08:00:00Z"
    assert versions[1]["fetched"] is False
    assert unknown.status_code == 404


async def test_the_registry_page_works_when_the_registry_is_off(
    admin, default_dataset, tmp_path
):
    admin["settings"] = settings_for(
        default_dataset, tmp_path, knowbase_dataset_registry_enabled=False
    )
    async with console() as http:
        got = await http.get(REGISTRY)
        history = await http.get(f"{REGISTRY}/{DATASET}/versions")
    assert got.json() == {"enabled": False, "datasets": []}
    assert history.status_code == 404
