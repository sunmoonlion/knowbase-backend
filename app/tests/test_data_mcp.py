"""数据 MCP（SDD 0048，第 2 步 2d）：令牌、按运行与会话读、查事实、找证据、出处。"""

from __future__ import annotations

import json
import time
from datetime import date
from decimal import Decimal

import httpx
import pytest
from fastapi import FastAPI
from joserfc import jwt
from joserfc.jwk import ECKey
from test_durable_delivery_db import db as db
from test_user_data import CSV, Clock, MemoryObjects

from app.application.services.call_rate import RateLimiter
from app.application.services.user_library_service import UserLibraryService
from app.application.services.user_resolution_service import UserResolutionService
from app.application.services.user_run_service import UserRunService
from app.domain import chart_of_accounts
from app.domain.public_access import PublicGate
from app.domain.user_facts import RawFact, StandardFact
from app.domain.user_library import PUBLIC_OWNER, Description
from app.domain.user_runs import GrantRequestItem, InputPin
from app.infrastructure.repositories.user_library import SqlUserLibraryRepository
from app.infrastructure.repositories.user_resolution import (
    SqlUserResolutionRepository,
)
from app.infrastructure.repositories.user_runs import SqlUserRunRepository
from app.interfaces.mcp import data_mcp
from core.config import Settings

KEY = ECKey.generate_key("P-256")
PAGES = [
    {"page": 1, "text": "贵州茅台酒股份有限公司 2025 年年度报告"},
    {"page": 88, "text": "报告期内渠道库存保持合理水平，营业收入同比增长。"},
]


def token(sub="alice", aud="knowbase-data", azp="knowbench", exp=None) -> str:
    claims = {
        "sub": sub,
        "aud": aud,
        "azp": azp,
        "sandbox": "sb-1",
        "iss": "knowbench",
        "exp": exp or int(time.time()) + 600,
    }
    return jwt.encode({"alg": "ES256"}, claims, KEY)


class World:
    def __init__(self, db, gate: PublicGate | None = None) -> None:
        clock = Clock()
        objects = MemoryObjects()
        library = SqlUserLibraryRepository(lambda: db)
        runs_repo = SqlUserRunRepository(lambda: db)
        self.lib = UserLibraryService(library, objects, prefix="p", clock=clock)
        self.runs = UserRunService(
            library, runs_repo, objects, prefix="p", clock=clock, gate=gate
        )
        self.resolver = UserResolutionService(
            SqlUserResolutionRepository(lambda: db),
            library,
            runs_repo,
            clock=clock,
            limiter=RateLimiter(1000),
            gate=gate,
        )
        server = data_mcp.DataMcp(
            lambda: self.runs, lambda: self.resolver, lambda: self.lib
        )
        settings = Settings(
            knowbase_mcp_jwt_public_key=KEY.as_pem(private=False).decode(),
            knowbase_mcp_jwt_issuer="knowbench",
        )
        app = FastAPI()
        app.include_router(data_mcp.build_router(settings, server))
        self.client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://knowbase"
        )

    async def call(self, name: str, auth: str | None = None, **arguments):
        reply = await self.client.post(
            "/mcp/data",
            headers={"Authorization": f"Bearer {auth or token()}"},
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {"name": name, "arguments": arguments},
            },
        )
        assert reply.status_code == 200, reply.text
        result = reply.json()["result"]
        if result["isError"]:
            return {"error": result["content"][0]["text"]}
        return result["structuredContent"]

    async def publish(self):
        material, version, _ = await self.lib.deliver_public(
            "knowfeed",
            "cninfo",
            "ar-600519-2025",
            "maotai-2025.csv",
            CSV,
            Description(
                subject="600519.SH",
                doc_type="annual_report",
                period_start=date(2025, 1, 1),
                period_end=date(2025, 12, 31),
            ),
        )
        grant = await self.runs.issue_processing(
            "knowfeed", PUBLIC_OWNER, material.id, 1
        )
        await self.runs.write_rendition(
            "knowfeed",
            grant.id,
            "pages",
            "p-0.1",
            json.dumps(PAGES, ensure_ascii=False).encode(),
        )
        raw = [
            RawFact(
                "营业收入",
                Decimal(1500),
                "万元",
                period_end=date(2025, 12, 31),
                duration="year",
                consolidation="consolidated",
                page=88,
            ),
            RawFact(
                "营业成本",
                Decimal(300),
                "万元",
                period_end=date(2025, 12, 31),
                duration="year",
                consolidation="consolidated",
                page=88,
            ),
        ]
        standard = [
            StandardFact(
                code,
                raw[i].value * 10_000,
                date(2025, 12, 31),
                "year",
                "consolidated",
                (i,),
                "chart-v1:alias",
                page=88,
            )
            for i, code in enumerate(("IS.REVENUE", "IS.COST"))
        ]
        await self.runs.write_facts("knowfeed", grant.id, "t-0.1", raw, standard)
        return material, version


@pytest.fixture
def world(db) -> World:
    return World(db)


async def test_tokens_must_be_issued_for_the_data_mcp(world):
    for bad in (token(aud="knowbase"), token(azp="knowmart"), token(exp=1)):
        reply = await world.client.post(
            "/mcp/data",
            headers={"Authorization": f"Bearer {bad}"},
            json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
        )
        assert reply.status_code == 401
    listed = await world.client.post(
        "/mcp/data",
        headers={"Authorization": f"Bearer {token()}"},
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
    )
    names = {t["name"] for t in listed.json()["result"]["tools"]}
    assert {"list_inputs", "read_input", "query_facts", "search_evidence"} <= names


async def test_a_run_reads_pinned_inputs_facts_and_evidence_with_sources(world):
    material, version = await world.publish()
    await world.runs.grant_by_user(
        "alice",
        "knowbench",
        "delegation",
        "task-1",
        [
            GrantRequestItem(
                InputPin(material.id, 1, version.sha256), "public", PUBLIC_OWNER
            )
        ],
    )
    inputs = (await world.call("list_inputs", run="task-1"))["inputs"]
    assert [(i["material_id"], i["owner"], i["readings"]) for i in inputs] == [
        (material.id, "public", ["pages", "table"])
    ]
    pages = await world.call(
        "read_input", run="task-1", material_id=material.id, what="pages", page_from=88
    )
    assert [p["page"] for p in pages["pages"]] == [88]
    assert pages["source"]["reading"] == "pages/1"
    facts = await world.call("query_facts", run="task-1", codes=["IS.REVENUE"])
    assert facts["facts"][0]["value"] == "15000000.000000"
    assert facts["facts"][0]["source"]["page"] == 88
    margin = await world.call("query_metric", metric="gross_margin", run="task-1")
    assert Decimal(margin["values"][0]["value"]) == Decimal("0.8")
    found = await world.call("search_evidence", run="task-1", query="渠道库存")
    assert found["passages"][0]["source"]["page"] == 88
    # 别人的令牌拿同一个 run：读不到
    other = await world.call("list_inputs", auth=token(sub="bob"), run="task-1")
    assert other["inputs"] == []
    stranger = await world.call(
        "read_input",
        auth=token(sub="bob"),
        run="task-1",
        material_id=material.id,
        what="pages",
    )
    assert "not an input" in stranger["error"]


async def test_market_lookup_and_sessions(world):
    material, _ = await world.publish()
    ranked = await world.call(
        "query_facts", scope="market", codes=["IS.REVENUE"], order="value_desc"
    )
    assert [f["subject"] for f in ranked["facts"]] == ["600519.SH"]
    seen = await world.call(
        "lookup",
        subject="600519.SH",
        doc_type="annual_report",
        period_start="2025-01-01",
        period_end="2025-12-31",
    )
    assert seen == {"status": "shown", "zone": "public"}
    session = await world.runs.register_session("alice", "knowbench")
    read = await world.call(
        "read_input", session=session.id, material_id=material.id, what="pages"
    )
    assert read["total_pages"] == 2
    evidence = await world.call("search_evidence", session=session.id, query="营业收入")
    assert evidence["passages"][0]["source"]["material_id"] == material.id
    library = await world.call("list_library", session=session.id)
    assert library == {"materials": []}  # 公开原件不在他名下，经 lookup 与读找
    items = await world.call("describe_items")
    assert items["version"] == chart_of_accounts.CHART_VERSION and any(
        m["key"] == "gross_margin" for m in items["metrics"]
    )


async def test_facts_carry_refs_that_read_back_within_the_same_scope(world):
    material, version = await world.publish()
    await world.runs.grant_by_user(
        "alice",
        "knowbench",
        "delegation",
        "task-2",
        [
            GrantRequestItem(
                InputPin(material.id, 1, version.sha256), "public", PUBLIC_OWNER
            )
        ],
    )
    facts = (await world.call("query_facts", run="task-2", codes=["IS.REVENUE"]))[
        "facts"
    ]
    ref = facts[0]["ref"]
    assert ref.startswith("fact:")
    raw = (await world.call("query_facts", run="task-2", layer="raw"))["facts"]
    assert all(r["ref"].startswith("raw:") for r in raw)
    back = await world.call("read_facts", run="task-2", refs=[ref, raw[0]["ref"]])
    assert [f["ref"] for f in back["facts"]] == [ref, raw[0]["ref"]]
    assert back["facts"][0]["value"] == "15000000.000000"
    assert back["not_found"] == []
    # 别人的同名 run、乱写的编号：读不到，照实回 not_found 或报错
    other = await world.call(
        "read_facts", auth=token(sub="bob"), run="task-2", refs=[ref]
    )
    assert other == {"facts": [], "not_found": [ref]}
    bad = await world.call("read_facts", run="task-2", refs=["cell:1"])
    assert "ref_invalid" in bad["error"]
    market = await world.call("read_facts", scope="market", refs=[ref])
    assert [f["ref"] for f in market["facts"]] == [ref]


async def test_a_session_queries_facts_of_what_it_has_read(world):
    material, _ = await world.publish()
    session = await world.runs.register_session("alice", "knowbench")
    before = await world.call("query_facts", session=session.id, codes=["IS.REVENUE"])
    assert before == {"facts": []}
    await world.call(
        "read_input", session=session.id, material_id=material.id, what="pages"
    )
    after = await world.call("query_facts", session=session.id, codes=["IS.REVENUE"])
    assert [f["subject"] for f in after["facts"]] == ["600519.SH"]
    margin = await world.call("query_metric", metric="gross_margin", session=session.id)
    assert Decimal(margin["values"][0]["value"]) == Decimal("0.8")
    stranger = await world.call(
        "query_facts", auth=token(sub="bob"), session=session.id, codes=["IS.REVENUE"]
    )
    assert "error" in stranger


async def test_text_reading_carries_title_and_date(world):
    """正文读法在库里一行：标题、发布日期、正文（0043 第五节）。"""
    material, version = await world.publish()
    grant = await world.runs.issue_processing("knowfeed", PUBLIC_OWNER, material.id, 1)
    await world.runs.write_rendition(
        "knowfeed",
        grant.id,
        "text",
        "t-0.1",
        "第一节 重要提示……".encode(),
        title="贵州茅台 2025 年年度报告",
        published_on=date(2026, 3, 28),
    )
    await world.runs.grant_by_user(
        "alice",
        "knowbench",
        "delegation",
        "task-t",
        [
            GrantRequestItem(
                InputPin(material.id, 1, version.sha256), "public", PUBLIC_OWNER
            )
        ],
    )
    text = await world.call(
        "read_input", run="task-t", material_id=material.id, what="text", offset=4
    )
    assert text["title"] == "贵州茅台 2025 年年度报告"
    assert text["published_on"] == "2026-03-28"
    assert (text["text"], text["more"]) == ("重要提示……", False)
    assert text["source"]["reading"] == "text/1"


async def test_market_scope_goes_through_the_one_gate(db):
    """跨全市场查事实过资格、权限、平台总开关那道门（0047 第 4.4 节，0048）。"""
    world = World(
        db, PublicGate(qualification="listed", qualified=frozenset({"alice"}))
    )
    await world.publish()
    ranked = await world.call("query_facts", scope="market", codes=["IS.REVENUE"])
    assert [f["subject"] for f in ranked["facts"]] == ["600519.SH"]
    for name, args in (
        ("query_facts", {"codes": ["IS.REVENUE"]}),
        ("query_metric", {"metric": "gross_margin"}),
        ("read_facts", {"refs": [ranked["facts"][0]["ref"]]}),
    ):
        refused = await world.call(name, auth=token(sub="bob"), scope="market", **args)
        assert refused["error"].startswith("qualification_required"), name


async def test_search_evidence_narrows_by_description_dimensions(world):
    """先圈后找：圈的那一步按描述的维度（所有者 10-27「保证到时可以按维度过滤
    检索」）。"""
    material, version = await world.publish()
    await world.runs.grant_by_user(
        "alice",
        "knowbench",
        "delegation",
        "task-3",
        [
            GrantRequestItem(
                InputPin(material.id, 1, version.sha256), "public", PUBLIC_OWNER
            )
        ],
    )
    everything = await world.call("search_evidence", run="task-3", query="渠道库存")
    assert everything["materials_searched"] == 1 and everything["passages"]
    for kept in (
        {"doc_type": "annual_report"},
        {"doc_type": "disclosure"},
        {"subjects": ["600519.SH"], "period_from": "2025-12-31"},
    ):
        found = await world.call(
            "search_evidence", run="task-3", query="渠道库存", **kept
        )
        assert found["materials_searched"] == 1, kept
    for dropped in (
        {"doc_type": "research"},
        {"subjects": ["000001.SZ"]},
        {"edition": "summary"},
        {"topics": ["dividend"]},
    ):
        found = await world.call(
            "search_evidence", run="task-3", query="渠道库存", **dropped
        )
        assert (found["materials_searched"], found["passages"]) == (0, []), dropped
    bad = await world.call("search_evidence", run="task-3", query="x", industry="白酒")
    assert "industry_invalid" in bad["error"]
    items = await world.call("describe_items")
    assert items["vocabulary"]["doc_classes"]["research"]["name"] == "研报"
