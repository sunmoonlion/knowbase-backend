"""语义层的开关（0009-semantic，SEM-01）：关着与以前一样；开着时工具走语义层。"""

from __future__ import annotations

import json

import httpx
import pytest
from fastapi import FastAPI
from test_knowledge_datasets import (
    TOKEN,
    VERSION,
    LocalFiles,
    MemoryRegistry,
    registered,
    settings_for,
)
from test_knowledge_datasets import default_dataset as default_dataset

from app.infrastructure.datasets import SqliteDatasetQueries
from app.infrastructure.semantic import SemanticDataset
from app.interfaces.errors.exception_handlers import register_exception_handlers
from app.interfaces.mcp.knowbase_mcp import (
    ALL_TOOLS,
    SEMANTIC_SQL_NOTE,
    KnowledgeMcp,
    build_router,
    tool_specs,
)


def server(default_dataset, tmp_path, *, semantic: bool) -> KnowledgeMcp:
    settings = settings_for(
        default_dataset,
        tmp_path,
        knowbase_semantic_engine_enabled=semantic,
        knowbase_semantic_cache_dir=str(tmp_path / "semantic"),
    )
    target = KnowledgeMcp(settings)
    # 登记表与对象存储用替身；打开数据集的方式用服务自己的
    target.catalog._registry = MemoryRegistry(registered())
    target.catalog._files = LocalFiles(tmp_path)
    return target


async def call(target: KnowledgeMcp, name: str, arguments: dict | None = None):
    grant = target.tokens.resolve(f"Bearer {TOKEN}")
    reply = await target.handle(
        grant,
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": name, "arguments": arguments or {}},
        },
    )
    result = reply["result"]
    return (
        result["isError"],
        result.get("structuredContent"),
        result["content"][0]["text"],
    )


async def tools(target: KnowledgeMcp) -> dict[str, dict]:
    grant = target.tokens.resolve(f"Bearer {TOKEN}")
    reply = await target.handle(
        grant, {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}
    )
    return {t["name"]: t for t in reply["result"]["tools"]}


def test_the_switch_is_off_by_default(default_dataset, tmp_path):
    settings = settings_for(default_dataset, tmp_path)
    assert settings.knowbase_semantic_engine_enabled is False
    target = KnowledgeMcp(settings)
    assert isinstance(target.dataset, SqliteDatasetQueries)
    assert target.tools is ALL_TOOLS
    assert tool_specs(semantic=False) is ALL_TOOLS


async def test_with_the_switch_off_everything_is_as_before(default_dataset, tmp_path):
    target = server(default_dataset, tmp_path, semantic=False)
    listed = await tools(target)
    assert set(listed) == {
        "list_datasets",
        "describe_schema",
        "metric_definitions",
        "run_sql",
    }
    assert "DuckDB" not in listed["run_sql"]["description"]
    error, data, _ = await call(
        target, "run_sql", {"sql": "SELECT SUM(amount) AS total FROM order_performance"}
    )
    assert not error and data["rows"] == [{"total": 350}]
    assert "engine" not in data["citation"]
    # 旧路上这是 SQLite：整数相除取整，系统表查得到
    error, data, _ = await call(target, "run_sql", {"sql": "SELECT 1/2 AS half"})
    assert not error and data["rows"] == [{"half": 0}]
    error, data, _ = await call(
        target, "run_sql", {"sql": "SELECT COUNT(*) AS n FROM sqlite_master"}
    )
    assert not error
    error, data, _ = await call(
        target, "describe_schema", {"table": "order_performance"}
    )
    assert data["tables"][0]["columns"][0] == {
        "name": "order_id",
        "type": "INTEGER",
        "primary_key": True,
    }
    assert not (tmp_path / "semantic").exists()


async def test_with_the_switch_on_the_tools_say_the_dialect_and_one_is_added(
    default_dataset, tmp_path
):
    target = server(default_dataset, tmp_path, semantic=True)
    assert isinstance(target.dataset, SemanticDataset)
    listed = await tools(target)
    assert set(listed) == set(ALL_TOOLS) | {"query_metric"}
    assert listed["run_sql"]["description"].endswith(SEMANTIC_SQL_NOTE)
    assert listed["run_sql"]["inputSchema"] == ALL_TOOLS["run_sql"]["inputSchema"]
    assert "DuckDB" not in ALL_TOOLS["run_sql"]["description"]  # 公共的那份没被改
    for name in ("list_datasets", "describe_schema", "metric_definitions"):
        assert listed[name] == {"name": name, **ALL_TOOLS[name]}


async def test_with_the_switch_on_both_datasets_go_through_the_semantic_layer(
    default_dataset, tmp_path
):
    target = server(default_dataset, tmp_path, semantic=True)
    error, data, _ = await call(
        target, "run_sql", {"sql": "SELECT SUM(amount) AS total FROM order_performance"}
    )
    assert not error and data["rows"] == [{"total": 350}]
    assert data["citation"]["dataset_id"] == "retail"
    assert data["citation"]["data_version"] == "retail-v1"
    assert data["citation"]["engine"] == "wrenai-0.15.0"
    error, data, _ = await call(
        target,
        "run_sql",
        {
            "dataset": "sh600009-financials",
            "sql": "SELECT fiscal_year, basis FROM income_statement "
            "WHERE report_type='年报' AND fiscal_year IN (2020, 2021) ORDER BY 1",
        },
    )
    assert not error and data["rows"] == [
        {"fiscal_year": 2020, "basis": "原始披露"},
        {"fiscal_year": 2021, "basis": "追溯调整后"},
    ]
    assert data["citation"]["data_version"] == VERSION
    error, data, _ = await call(
        target,
        "describe_schema",
        {"dataset": "sh600009-financials", "table": "cash_flow"},
    )
    assert not error and data["sql_dialect"] == "duckdb"
    error, data, _ = await call(
        target,
        "metric_definitions",
        {"dataset": "sh600009-financials", "metric": "毛利率"},
    )
    assert not error and [m["metric_name"] for m in data["metrics"]] == ["gross_margin"]
    error, data, _ = await call(target, "list_datasets")
    assert [d["dataset"] for d in data["datasets"]] == ["retail", "sh600009-financials"]
    assert len(list((tmp_path / "semantic").glob("*.duckdb"))) == 2


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT COUNT(*) AS n FROM sqlite_master",
        "SELECT COUNT(*) AS n FROM ds.main.phys_income_statement",
        "SELECT COUNT(*) AS n FROM phys_income_statement",
        "SELECT * FROM read_text('/etc/hostname')",
        "DELETE FROM income_statement",
        "PRAGMA database_list",
    ],
)
async def test_with_the_switch_on_refusals_are_tool_errors(
    default_dataset, tmp_path, sql
):
    target = server(default_dataset, tmp_path, semantic=True)
    error, data, text = await call(
        target, "run_sql", {"dataset": "sh600009-financials", "sql": sql}
    )
    assert error and data is None
    assert ".duckdb" not in text and str(tmp_path) not in text


async def test_a_dataset_the_semantic_layer_cannot_serve_is_unavailable(
    default_dataset, tmp_path
):
    import sqlite3

    broken = tmp_path / "broken.sqlite3"
    with sqlite3.connect(broken) as c:
        c.executescript("CREATE TABLE phys_orders(id INTEGER);")
    target = server(default_dataset, tmp_path, semantic=True)
    target.catalog._files = LocalFiles(tmp_path, content=broken.read_bytes())
    error, _, text = await call(
        target, "run_sql", {"dataset": "sh600009-financials", "sql": "SELECT 1"}
    )
    assert error and text == "dataset unavailable"


async def test_over_http_with_the_switch_on(default_dataset, tmp_path):
    settings = settings_for(
        default_dataset,
        tmp_path,
        knowbase_semantic_engine_enabled=True,
        knowbase_semantic_cache_dir=str(tmp_path / "semantic"),
        knowbase_dataset_registry_enabled=False,
    )
    app = FastAPI()
    register_exception_handlers(app)
    app.include_router(build_router(settings), prefix="/api")
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://kb"
    ) as client:
        response = await client.post(
            "/api/mcp/knowledge",
            json={
                "jsonrpc": "2.0",
                "id": 7,
                "method": "tools/call",
                "params": {
                    "name": "run_sql",
                    "arguments": {"sql": "SELECT 7 // 2 AS q, 1 / 2 AS half"},
                },
            },
            headers={"Authorization": f"Bearer {TOKEN}"},
        )
    assert response.status_code == 200
    result = response.json()["result"]
    assert result["isError"] is False
    assert result["structuredContent"]["rows"] == [{"q": 3, "half": 0.5}]
    assert json.loads(result["content"][0]["text"])["rows"] == [{"q": 3, "half": 0.5}]
    assert len(list((tmp_path / "semantic").glob("*.duckdb"))) == 1
