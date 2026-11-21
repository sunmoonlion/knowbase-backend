"""数据 MCP（SDD 0048，第 2 步 2d）：令牌、按运行与会话读、查事实、找证据、出处。"""

from __future__ import annotations

import hashlib
import json
import time
from datetime import date
from decimal import Decimal
from uuid import uuid4

import httpx
import pytest
from fastapi import FastAPI
from joserfc import jwt
from joserfc.jwk import ECKey
from test_durable_delivery_db import db as db
from test_user_data import CSV, Clock, MemoryObjects

from app.application.services.call_rate import RateLimiter
from app.application.services.data_functions import TOOLS, DataFunctions, DataToken
from app.application.services.query_facts_revision import query_facts_revision
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
from app.interfaces.http.internal.function_calls import build_function_router
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
        "tools": sorted(set(TOOLS) - {"read_method_input"}),
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
        self.functions = DataFunctions(
            lambda: self.runs, lambda: self.resolver, lambda: self.lib
        )
        settings = Settings(
            knowbase_mcp_jwt_public_key=KEY.as_pem(private=False).decode(),
            knowbase_mcp_jwt_issuer="knowbench",
        )
        app = FastAPI()
        app.include_router(
            data_mcp.build_router(settings, data_mcp.DataMcp(self.functions))
        )
        app.include_router(build_function_router(settings, self.functions))
        self.client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://localhost"
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

    async def facts(self, *, sub="alice", **arguments):
        arguments.setdefault("scope", "run")
        call_id = str(uuid4())
        digest = query_facts_revision()["digest"]
        claims = dict(jwt.decode(token(sub=sub, aud="knowbase-function"), KEY).claims)
        claims.update(
            call_id=call_id,
            function_digest=digest,
            arguments_digest=hashlib.sha256(
                json.dumps(
                    arguments, sort_keys=True, separators=(",", ":"), ensure_ascii=False
                ).encode()
            ).hexdigest(),
        )
        response = await self.client.post(
            "/internal/v1/functions/query_facts/1",
            json={"call_id": call_id, "digest": digest, "arguments": arguments},
            headers={
                "Authorization": "Bearer " + jwt.encode({"alg": "ES256"}, claims, KEY)
            },
        )
        if response.status_code != 200:
            return {"error": response.json()["detail"]}
        return response.json()["output"]

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
    assert {"list_inputs", "read_input", "search_evidence"} <= names
    assert "query_facts" not in names
    obsolete = await world.client.post(
        "/mcp/data",
        headers={"Authorization": f"Bearer {token()}"},
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "query_facts", "arguments": {"scope": "market"}},
        },
    )
    assert obsolete.json()["error"]["code"] == -32602


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
    facts = await world.facts(run="task-1", codes=["IS.REVENUE"])
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
    ranked = await world.facts(scope="market", codes=["IS.REVENUE"], order="value_desc")
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
    facts = (await world.facts(run="task-2", codes=["IS.REVENUE"]))["facts"]
    ref = facts[0]["ref"]
    assert ref.startswith("fact:")
    raw = (await world.facts(run="task-2", layer="raw"))["facts"]
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
    before = await world.facts(session=session.id, codes=["IS.REVENUE"])
    assert before == {"facts": []}
    await world.call(
        "read_input", session=session.id, material_id=material.id, what="pages"
    )
    after = await world.facts(session=session.id, codes=["IS.REVENUE"])
    assert [f["subject"] for f in after["facts"]] == ["600519.SH"]
    margin = await world.call("query_metric", metric="gross_margin", session=session.id)
    assert Decimal(margin["values"][0]["value"]) == Decimal("0.8")
    stranger = await world.facts(sub="bob", session=session.id, codes=["IS.REVENUE"])
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
    ranked = await world.facts(scope="market", codes=["IS.REVENUE"])
    assert [f["subject"] for f in ranked["facts"]] == ["600519.SH"]
    assert "error" in await world.facts(sub="bob", scope="market")
    for name, args in (
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


async def test_owner_pinned_method_read_survives_restart_and_rechecks_revocation(
    world, db, monkeypatch
):
    from datetime import timedelta
    from uuid import uuid4

    from app.domain.method_reads import MethodReadConsent
    from app.domain.user_data import UserDataError

    material, version = await world.lib.upload(
        "alice", "example.txt", b"one pinned text"
    )
    await world.lib.show("alice", material.id, "app:knowbench")
    task_id = str(uuid4())
    parent = await world.runs.grant_by_user(
        "alice",
        "knowbench",
        "delegation",
        "formal-task:" + task_id,
        [
            GrantRequestItem(
                InputPin(material.id, version.version_no, version.sha256),
                "display",
                "app:knowbench",
            )
        ],
    )
    args = {
        "material_id": material.id,
        "version_no": version.version_no,
    }
    scope = {
        "task_id": task_id,
        "material_id": material.id,
        "version_no": version.version_no,
        "purpose": "Read the pinned report for this comparison",
        "path": [
            {
                "method_id": str(uuid4()),
                "call_id": str(uuid4()),
                "definition_digest": "sha256:" + str(i) * 64,
                "version": i or None,
            }
            for i in range(3)
        ],
    }
    consent = MethodReadConsent.model_validate(
        {"read": scope, "expires_at": parent.expires_at}
    )

    def bearer(binding):
        claims = dict(jwt.decode(token(), KEY).claims)
        claims.update(tools=["read_method_input"], method_read=binding)
        return jwt.encode({"alg": "ES256"}, claims, KEY)

    async def read(binding, params=None):
        return await world.call(
            "read_method_input", auth=bearer(binding), **(params or args)
        )

    assert "error" in await read(scope)
    # An otherwise valid parent grant from a different task cannot be rebound.
    other_grant = await world.runs.grant_by_user(
        "alice",
        "knowbench",
        "delegation",
        "other-task",
        [
            GrantRequestItem(
                InputPin(material.id, version.version_no, version.sha256),
                "display",
                "app:knowbench",
            )
        ],
    )
    with pytest.raises(UserDataError):
        await world.runs.consent_method_read(
            "alice", "knowbench", other_grant.id, consent
        )
    assert "error" in await world.call("read_method_input", **args)
    with pytest.raises(UserDataError):
        await world.runs.consent_method_read("bob", "knowbench", parent.id, consent)
    with pytest.raises(UserDataError):
        await world.runs.consent_method_read(
            "alice",
            "knowbench",
            parent.id,
            consent.model_copy(
                update={"expires_at": parent.expires_at + timedelta(seconds=1)}
            ),
        )
    await world.runs.consent_method_read("alice", "knowbench", parent.id, consent)
    await world.runs.consent_method_read("alice", "knowbench", parent.id, consent)
    # Rebuild services, retaining only PostgreSQL and stored objects.
    restarted = World(db)
    restarted.runs.objects = world.runs.objects
    world = restarted
    # Upload enforces UTF-8. Simulate an older storage adapter returning
    # authorized bytes in a different encoding through the real MCP route.
    from dataclasses import replace

    get_read = world.runs.read_for_method

    async def legacy_read(*args):
        result, grant_id = await get_read(*args)
        return replace(result, content="中文".encode("gbk")), grant_id

    with monkeypatch.context() as patch:
        patch.setattr(world.runs, "read_for_method", legacy_read)
        assert "method_input_encoding" in (await read(scope))["error"]
    assert (await read(scope))["text"] == "one pinned text"
    for changed in ({"material_id": str(uuid4())}, {"version_no": 2}):
        assert "error" in await read(scope, {**args, **changed})
    for changed in (
        {"purpose": "another use"},
        {"task_id": str(uuid4())},
        {"path": scope["path"][1:]},
    ):
        assert "error" in await read({**scope, **changed})
    leaf = scope["path"][-1]["call_id"]
    with pytest.raises(UserDataError):
        await world.runs.revoke_method_read("bob", "knowbench", parent.id, leaf)
    await world.runs.revoke_method_read("alice", "knowbench", parent.id, leaf)
    assert "error" in await read(scope)
    with pytest.raises(UserDataError):
        await world.runs.consent_method_read("alice", "knowbench", parent.id, consent)
    new_path = [*scope["path"][:-1], {**scope["path"][-1], "call_id": str(uuid4())}]
    second = {**scope, "path": new_path}
    await world.runs.consent_method_read(
        "alice",
        "knowbench",
        parent.id,
        MethodReadConsent.model_validate(
            {"read": second, "expires_at": parent.expires_at}
        ),
    )
    assert (await read(second))["text"] == "one pinned text"
    early = {
        **scope,
        "path": [*new_path[:-1], {**new_path[-1], "call_id": str(uuid4())}],
    }
    expires = world.runs.now() + timedelta(seconds=1)
    await world.runs.consent_method_read(
        "alice",
        "knowbench",
        parent.id,
        MethodReadConsent.model_validate({"read": early, "expires_at": expires}),
    )
    assert (await read(early))["text"] == "one pinned text"
    world.runs.now = lambda: expires
    assert "error" in await read(early)
    assert (await read(second))["text"] == "one pinned text"
    await world.runs.runs.revoke([parent.id], world.runs.now(), "test")
    assert "error" in await read(second)


async def test_exact_method_parent_is_idempotent_and_stays_revoked(world):
    import asyncio
    from datetime import timedelta
    from uuid import uuid4

    from app.domain.method_reads import MethodParentRequest
    from app.domain.user_data import UserDataError

    material, version = await world.lib.upload("alice", "report.txt", b"approved")
    await world.lib.show("alice", material.id, "app:knowbench")
    request = MethodParentRequest.model_validate(
        {
            "read": {
                "task_id": str(uuid4()),
                "path": [
                    {
                        "call_id": str(uuid4()),
                        "method_id": str(uuid4()),
                        "definition_digest": "sha256:" + "a" * 64,
                    }
                ],
                "material_id": material.id,
                "version_no": 1,
                "purpose": "read for this task",
            },
            "expires_at": world.runs.now() + timedelta(minutes=5),
            "sha256": version.sha256,
        }
    )
    with pytest.raises(UserDataError):
        await world.runs.method_parent_grant("bob", "knowbench", request)
    with pytest.raises(UserDataError):
        await world.runs.method_parent_grant(
            "alice", "knowbench", request.model_copy(update={"sha256": "0" * 64})
        )
    first, again = await asyncio.gather(
        *[
            world.runs.method_parent_grant("alice", "knowbench", request)
            for _ in range(2)
        ]
    )
    assert first.id == again.id and first.expires_at == request.expires_at
    from uuid import uuid4

    later_leaf = request.read.path[-1].model_copy(
        update={"call_id": uuid4(), "version": 1}
    )
    second_read = request.read.model_copy(
        update={"path": (*request.read.path, later_leaf)}
    )
    later_request = request.model_copy(
        update={
            "read": second_read,
            "expires_at": request.expires_at + timedelta(minutes=1),
        }
    )
    parent = await world.runs.method_parent_grant("alice", "knowbench", later_request)
    assert (parent.id, parent.expires_at) == (first.id, first.expires_at)
    with pytest.raises(UserDataError):
        await world.runs.consent_method_read(
            "alice", "knowbench", parent.id, later_request
        )
    await world.runs.consent_method_read(
        "alice",
        "knowbench",
        parent.id,
        later_request.model_copy(update={"expires_at": parent.expires_at}),
    )
    assert first.method_only
    assert not await world.runs.run_grants("knowbench", "alice", first.ref)
    with pytest.raises(UserDataError):
        await world.runs.read(
            "knowbench", "regular-run", first.id, material.id, 1, "content"
        )
    assert "error" in await world.call(
        "read_input",
        run=first.ref,
        material_id=material.id,
        version_no=1,
        what="original",
    )
    await world.runs.consent_method_read("alice", "knowbench", first.id, request)
    assert (await world.runs.read_for_method("alice", request.read))[
        0
    ].content == b"approved"
    await world.runs.runs.revoke([first.id], world.runs.now(), "test")
    with pytest.raises(UserDataError):
        await world.runs.method_parent_grant("alice", "knowbench", request)


async def test_direct_facts_match_granted_rows_and_empty_scope_denies(
    world,
):
    from jsonschema import Draft202012Validator

    from app.application.services.data_functions import DataFunctionError
    from app.application.services.query_facts_revision import query_facts_contract

    material, version = await world.publish()
    await world.runs.grant_by_user(
        "alice",
        "knowbench",
        "delegation",
        "direct-test",
        [
            GrantRequestItem(
                InputPin(material.id, 1, version.sha256), "public", PUBLIC_OWNER
            )
        ],
    )
    args = {"scope": "run", "run": "direct-test", "codes": ["IS.REVENUE"]}
    allowed = DataToken("alice", "knowbench", "sb-1", frozenset({"query_facts"}))
    direct = await world.functions.invoke(allowed, "query_facts", args)
    reply = await world.facts(**args)
    published = query_facts_contract()
    Draft202012Validator(published["input_schema"]).validate(args)
    Draft202012Validator(published["output_schema"]).validate(reply)
    assert direct["facts"] and [item["ref"] for item in direct["facts"]] == [
        item["ref"] for item in reply["facts"]
    ]

    for tools in ([], None):
        claims = dict(jwt.decode(token(), KEY).claims)
        claims.pop("tools")
        if tools is not None:
            claims["tools"] = tools
        encoded = jwt.encode({"alg": "ES256"}, claims, KEY)
        empty = data_mcp.verify("Bearer " + encoded, KEY, "knowbench")
        assert empty is not None and not empty.tools
        with pytest.raises(DataFunctionError, match="not allowed"):
            await world.functions.invoke(empty, "query_facts", args)
