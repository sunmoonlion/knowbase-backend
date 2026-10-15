# ruff: noqa: E501
"""知识 MCP：鉴权、工具过滤、只读 SQL、引证、限流（0006 F-KNOW-01/02/03/05）。"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import httpx
import pytest
from joserfc import jwt as jose_jwt
from joserfc.jwk import ECKey

from app.application.services.dataset_query import SqlRejected
from app.bootstrap.mcp import create_mcp_app
from app.infrastructure.datasets import SqliteDatasetQueries
from core.config import Settings

TOKEN_FULL = "tok-full-0123456789abcdef"
TOKEN_SCHEMA_ONLY = "tok-schema-0123456789abcdef"


@pytest.fixture
def dataset(tmp_path: Path) -> Path:
    path = tmp_path / "mini.sqlite"
    with sqlite3.connect(path) as c:
        c.executescript(
            """
            CREATE TABLE dataset_metadata(key TEXT PRIMARY KEY, value TEXT);
            INSERT INTO dataset_metadata VALUES
              ('data_snapshot_id','mini-v1'),('start_date','2024-01-01'),('end_date','2025-12-31');
            CREATE TABLE metric_dictionary(metric_name TEXT, display_name TEXT, source_table TEXT,
              expression_hint TEXT, unit TEXT, time_basis TEXT, description TEXT);
            INSERT INTO metric_dictionary VALUES
              ('net_revenue_cents','净营收','order_performance','SUM(net_revenue_cents)','分','下单日','扣退款后');
            CREATE TABLE order_performance(order_id INTEGER PRIMARY KEY, order_year INTEGER, net_revenue_cents INTEGER);
            """
        )
        c.executemany(
            "INSERT INTO order_performance VALUES (?,?,?)",
            [(i, 2024 + (i % 2), 100 * i) for i in range(1, 301)],
        )
    return path


@pytest.fixture
def settings(dataset: Path) -> Settings:
    return Settings(
        _env_file=None,
        knowbase_dataset_path=str(dataset),
        knowbase_dataset_id="mini",
        knowbase_mcp_tokens_json=json.dumps(
            {
                TOKEN_FULL: {"user": "u1", "sandbox": "sb1"},
                TOKEN_SCHEMA_ONLY: {
                    "user": "u2",
                    "sandbox": "sb2",
                    "tools": ["describe_schema"],
                },
            }
        ),
        knowbase_mcp_rate_per_minute=5,
    )


@pytest.fixture
async def client(settings: Settings):
    app = create_mcp_app(settings)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://kb"
    ) as c:
        yield c


def rpc(method: str, params: dict | None = None, mid: int | None = 1) -> dict:
    msg: dict = {"jsonrpc": "2.0", "method": method, "params": params or {}}
    if mid is not None:
        msg["id"] = mid
    return msg


async def post(client: httpx.AsyncClient, token: str | None, body) -> httpx.Response:
    headers = {"Accept": "application/json, text/event-stream"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return await client.post("/api/mcp/knowledge", json=body, headers=headers)


async def test_requires_bearer_token(client):
    r = await post(client, None, rpc("initialize"))
    assert r.status_code == 401 and r.headers["WWW-Authenticate"] == "Bearer"
    r = await post(client, "tok-unknown", rpc("initialize"))
    assert r.status_code == 401


async def test_initialize_and_notification(client):
    r = await post(
        client, TOKEN_FULL, rpc("initialize", {"protocolVersion": "2025-03-26"})
    )
    assert r.status_code == 200
    result = r.json()["result"]
    assert result["protocolVersion"] == "2025-03-26"
    assert result["serverInfo"]["name"] == "sunmoon-knowbase"
    r = await post(client, TOKEN_FULL, rpc("notifications/initialized", mid=None))
    assert r.status_code == 202
    r = await client.get(
        "/api/mcp/knowledge", headers={"Authorization": f"Bearer {TOKEN_FULL}"}
    )
    assert r.status_code == 405


async def test_tools_list_is_filtered_by_token(client):
    r = await post(client, TOKEN_FULL, rpc("tools/list"))
    assert {t["name"] for t in r.json()["result"]["tools"]} == {
        "list_datasets",
        "describe_schema",
        "metric_definitions",
        "run_sql",
    }
    r = await post(client, TOKEN_SCHEMA_ONLY, rpc("tools/list"))
    assert [t["name"] for t in r.json()["result"]["tools"]] == ["describe_schema"]


async def test_forbidden_tool_is_a_tool_error_and_counted(client, settings):
    r = await post(
        client,
        TOKEN_SCHEMA_ONLY,
        rpc("tools/call", {"name": "run_sql", "arguments": {"sql": "select 1"}}),
    )
    body = r.json()["result"]
    assert body["isError"] is True and "not allowed" in body["content"][0]["text"]


async def test_run_sql_returns_rows_citation_and_truncation(client):
    r = await post(
        client,
        TOKEN_FULL,
        rpc(
            "tools/call",
            {
                "name": "run_sql",
                "arguments": {
                    "sql": "SELECT order_year, SUM(net_revenue_cents) AS net FROM order_performance GROUP BY order_year ORDER BY order_year;"
                },
            },
        ),
    )
    result = r.json()["result"]
    assert result["isError"] is False
    data = result["structuredContent"]
    assert data["columns"] == ["order_year", "net"]
    assert data["rows"] == [
        {"order_year": 2024, "net": 100 * sum(i for i in range(1, 301) if i % 2 == 0)},
        {"order_year": 2025, "net": 100 * sum(i for i in range(1, 301) if i % 2 == 1)},
    ]
    assert data["truncated"] is False
    assert data["citation"]["data_version"] == "mini-v1"
    assert data["citation"]["dataset_id"] == "mini"
    assert data["citation"]["as_of"] == "2025-12-31"
    assert len(data["citation"]["query_digest"]) == 16

    r = await post(
        client,
        TOKEN_FULL,
        rpc(
            "tools/call",
            {
                "name": "run_sql",
                "arguments": {"sql": "SELECT * FROM order_performance"},
            },
        ),
    )
    data = r.json()["result"]["structuredContent"]
    assert data["row_count"] == 200 and data["truncated"] is True


@pytest.mark.parametrize(
    "sql",
    [
        "UPDATE order_performance SET net_revenue_cents = 0",
        "SELECT 1; SELECT 2",
        "PRAGMA table_info(order_performance)",
        "ATTACH DATABASE '/etc/passwd' AS x",
        "",
        "DELETE FROM order_performance",
    ],
)
async def test_run_sql_rejects_non_readonly(client, sql):
    r = await post(
        client,
        TOKEN_FULL,
        rpc("tools/call", {"name": "run_sql", "arguments": {"sql": sql}}),
    )
    assert r.json()["result"]["isError"] is True


def test_guard_blocks_writes_even_through_sqlite(dataset: Path):
    svc = SqliteDatasetQueries(dataset, dataset_id="mini")
    with pytest.raises(SqlRejected):
        svc.guard("INSERT INTO order_performance VALUES (9999, 2024, 1)")
    # 即便绕过 guard，连接也是只读的
    with pytest.raises(sqlite3.OperationalError):
        with svc._connect() as conn:
            conn.execute("INSERT INTO order_performance VALUES (9999, 2024, 1)")


async def test_schema_and_metrics_carry_data_version(client):
    r = await post(
        client,
        TOKEN_FULL,
        rpc(
            "tools/call",
            {"name": "describe_schema", "arguments": {"table": "order_performance"}},
        ),
    )
    data = r.json()["result"]["structuredContent"]
    assert data["tables"][0]["row_count"] == 300
    assert [c["name"] for c in data["tables"][0]["columns"]] == [
        "order_id",
        "order_year",
        "net_revenue_cents",
    ]
    assert data["citation"]["data_version"] == "mini-v1"
    r = await post(
        client,
        TOKEN_FULL,
        rpc("tools/call", {"name": "metric_definitions", "arguments": {}}),
    )
    data = r.json()["result"]["structuredContent"]
    assert data["metrics"][0]["metric_name"] == "net_revenue_cents"
    assert data["citation"]["data_version"] == "mini-v1"


async def test_rate_limit_per_token(client):
    for _ in range(5):
        r = await post(client, TOKEN_FULL, rpc("ping"))
        assert r.status_code == 200
    # ping 不计数；工具调用计数
    for i in range(5):
        r = await post(
            client,
            TOKEN_FULL,
            rpc("tools/call", {"name": "describe_schema", "arguments": {}}),
        )
        assert r.json()["result"]["isError"] is False, i
    r = await post(
        client,
        TOKEN_FULL,
        rpc("tools/call", {"name": "describe_schema", "arguments": {}}),
    )
    assert (
        r.json()["result"]["isError"] is True
        and "rate limit" in r.json()["result"]["content"][0]["text"]
    )


async def test_batch_and_unknown_method(client):
    r = await post(client, TOKEN_FULL, [rpc("ping", mid=1), rpc("nope", mid=2)])
    replies = r.json()
    assert isinstance(replies, list) and len(replies) == 2
    assert replies[1]["error"]["code"] == -32601


# ---- D10：工作台签发的 JWT ----
SIGNING_KEY = ECKey.generate_key("P-256")


def mint(claims: dict, key: ECKey = SIGNING_KEY) -> str:
    base = {
        "iss": "wb-test",
        "aud": "knowbase",
        "sub": "u-jwt",
        "sandbox": "u-jwt",
        "exp": 4102444800,
    }
    return jose_jwt.encode({"alg": "ES256"}, {**base, **claims}, key)


@pytest.fixture
def jwt_settings(settings: Settings) -> Settings:
    return settings.model_copy(
        update={
            "knowbase_mcp_jwt_public_key": SIGNING_KEY.as_pem(private=False).decode(),
            "knowbase_mcp_jwt_issuer": "wb-test",
        }
    )


@pytest.fixture
async def jwt_client(jwt_settings: Settings):
    app = create_mcp_app(jwt_settings)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://kb"
    ) as c:
        yield c


async def test_jwt_grants_and_rejections(jwt_client):
    ok = mint({})
    r = await post(jwt_client, ok, rpc("tools/list"))
    assert r.status_code == 200 and len(r.json()["result"]["tools"]) == 4
    limited = mint({"tools": ["describe_schema", "not_a_tool"]})
    r = await post(jwt_client, limited, rpc("tools/list"))
    assert [t["name"] for t in r.json()["result"]["tools"]] == ["describe_schema"]
    # 静态表在有公钥时仍然有效（两种共存）
    assert (await post(jwt_client, TOKEN_FULL, rpc("tools/list"))).status_code == 200
    for bad in (
        mint({"aud": "relay"}),
        mint({"iss": "someone-else"}),
        mint({"exp": 1}),
        mint({}, ECKey.generate_key("P-256")),
        ok[:-4] + "AAAA",
        mint({"tools": "run_sql"}),
    ):
        assert (await post(jwt_client, bad, rpc("tools/list"))).status_code == 401, bad[
            :20
        ]


async def test_jwt_is_ignored_without_public_key(client):
    assert (await post(client, mint({}), rpc("tools/list"))).status_code == 401


def test_metric_lookup_by_display_name_and_substring(dataset):
    svc = SqliteDatasetQueries(dataset, dataset_id="mini")
    by_name = svc.metric_definitions("net_revenue_cents")["metrics"]
    by_display = svc.metric_definitions("净营收")["metrics"]
    assert [m["metric_name"] for m in by_name] == ["net_revenue_cents"]
    assert by_display == by_name
    assert [m["metric_name"] for m in svc.metric_definitions("营收")["metrics"]] == [
        "net_revenue_cents"
    ]
    assert [
        m["metric_name"] for m in svc.metric_definitions("NET_REVENUE")["metrics"]
    ] == ["net_revenue_cents"]
    assert svc.metric_definitions("毛利率")["metrics"] == []
    assert len(svc.metric_definitions()["metrics"]) == 1
