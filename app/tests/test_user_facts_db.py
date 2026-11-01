"""结构化财务数据（SDD 0047 第四节，第 2 步 2c）。

写回原样层与标准化层、恒等式质检、按运行查、跨全市场筛选排名、指标、出处到页。
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest
from test_durable_delivery_db import db as db
from test_user_data import CSV, Clock, MemoryObjects

from app.application.services.user_library_service import UserLibraryService
from app.application.services.user_run_service import UserRunService
from app.domain.public_access import PublicGate
from app.domain.user_data import UserDataError
from app.domain.user_facts import FactQuery, RawFact, StandardFact
from app.domain.user_library import PUBLIC_OWNER, Description
from app.domain.user_runs import GrantRequestItem, InputPin
from app.infrastructure.repositories.user_library import SqlUserLibraryRepository
from app.infrastructure.repositories.user_runs import SqlUserRunRepository

ALICE = "alice"
BOB = "bob"
Y2025, Y2024 = date(2025, 12, 31), date(2024, 12, 31)


def report(subject: str) -> Description:
    return Description(
        subject=subject,
        doc_type="annual_report",
        period_start=date(2025, 1, 1),
        period_end=Y2025,
        disclosed_on=date(2026, 3, 28),
    )


def statements(revenue: int, cost: int, prior_revenue: int, *, assets=1000, ok=True):
    """一份年报抽出来的几行：利润表本期与上年同期（万元），资产负债表期末（万元）。"""
    raw = [
        RawFact(
            "营业收入",
            Decimal(revenue),
            "万元",
            period_end=Y2025,
            duration="year",
            consolidation="consolidated",
            statement_label="合并利润表",
            page=88,
        ),
        RawFact(
            "营业收入",
            Decimal(prior_revenue),
            "万元",
            period_end=Y2024,
            duration="year",
            consolidation="consolidated",
            statement_label="合并利润表",
            page=88,
        ),
        RawFact(
            "营业成本",
            Decimal(cost),
            "万元",
            period_end=Y2025,
            duration="year",
            consolidation="consolidated",
            statement_label="合并利润表",
            page=88,
        ),
        RawFact(
            "资产总计",
            Decimal(assets),
            "万元",
            period_end=Y2025,
            duration="instant",
            consolidation="consolidated",
            page=84,
        ),
        RawFact(
            "负债合计",
            Decimal(400),
            "万元",
            period_end=Y2025,
            duration="instant",
            consolidation="consolidated",
            page=85,
        ),
        RawFact(
            "所有者权益合计",
            Decimal(600 if ok else 500),
            "万元",
            period_end=Y2025,
            duration="instant",
            consolidation="consolidated",
            page=85,
        ),
    ]
    codes = [
        ("IS.REVENUE", Y2025, "year", 0),
        ("IS.REVENUE", Y2024, "year", 1),
        ("IS.COST", Y2025, "year", 2),
        ("BS.TOTAL_ASSETS", Y2025, "instant", 3),
        ("BS.TOTAL_LIABILITIES", Y2025, "instant", 4),
        ("BS.TOTAL_EQUITY", Y2025, "instant", 5),
    ]
    standard = [
        StandardFact(
            code,
            raw[i].value * 10_000,
            period,
            duration,
            "consolidated",
            (i,),
            "chart-v1:alias",
            page=raw[i].page,
        )
        for code, period, duration, i in codes
    ]
    return raw, standard


class World:
    def __init__(self, db, *, gate: PublicGate | None = None) -> None:
        clock = Clock()
        objects = MemoryObjects()
        library = SqlUserLibraryRepository(lambda: db)
        self.lib = UserLibraryService(library, objects, prefix="p", clock=clock)
        self.runs = UserRunService(
            library,
            SqlUserRunRepository(lambda: db),
            objects,
            prefix="p",
            clock=clock,
            gate=gate,
        )

    async def publish(self, subject: str, revenue: int, cost: int, prior: int, **kw):
        material, version, _ = await self.lib.deliver_public(
            "knowfeed",
            "cninfo",
            f"ar-{subject}",
            f"{subject}-2025.csv",
            CSV + subject.encode(),
            report(subject),
        )
        grant = await self.runs.issue_processing(
            "knowfeed", PUBLIC_OWNER, material.id, version.version_no
        )
        raw, standard = statements(revenue, cost, prior, **kw)
        table = await self.runs.write_facts(
            "knowfeed", grant.id, "ar-0.1", raw, standard
        )
        return material, version, table


@pytest.fixture
def world(db) -> World:
    return World(db)


async def refused(work, status: int, code: str) -> None:
    with pytest.raises(UserDataError) as caught:
        await work
    assert (caught.value.status, caught.value.code) == (status, code)


async def test_facts_are_a_table_reading_with_quality_recorded(world):
    material, version, table = await world.publish("600519.SH", 1500, 300, 1300)
    assert (table.kind, table.status, table.rendition_no) == ("table", "ready", 1)
    assert table.detail["quality_passed"] is True
    assert {c["check"] for c in table.detail["quality"]} == {
        "assets_equal_liabilities_plus_equity"
    }
    # 恒等式对不上：照实记，不改数
    _, _, broken = await world.publish("000858.SZ", 900, 400, 800, ok=False)
    assert broken.detail["quality_passed"] is False
    assert Decimal(broken.detail["quality"][0]["diff"]) == Decimal(1_000_000)

    rows = await world.runs.query_market(
        ALICE, FactQuery(subjects=("600519.SH",), codes=("IS.REVENUE",))
    )
    assert [(r.period_end, r.value, r.page, r.unit) for r in rows] == [
        (Y2025, Decimal("15000000.000000"), 88, "元"),
        (Y2024, Decimal("13000000.000000"), 88, "元"),
    ]
    assert (rows[0].material_id, rows[0].rendition_no) == (material.id, 1)
    raw = await world.runs.query_market(
        ALICE, FactQuery(layer="raw", subjects=("600519.SH",), labels=("营业成本",))
    )
    assert [(r.label, r.value, r.unit) for r in raw] == [
        ("营业成本", Decimal("300.000000"), "万元")
    ]


async def test_market_screening_ranking_and_metrics(world):
    await world.publish("600519.SH", 1500, 300, 1300)
    await world.publish("000858.SZ", 900, 400, 800)
    await world.publish("000568.SZ", 400, 100, 350)
    ranked = await world.runs.query_market(
        ALICE, FactQuery(codes=("IS.REVENUE",), period_from=Y2025, order="value_desc")
    )
    assert [r.subject for r in ranked] == ["600519.SH", "000858.SZ", "000568.SZ"]
    screened = await world.runs.query_market(
        ALICE,
        FactQuery(
            codes=("IS.REVENUE",), period_from=Y2025, value_min=Decimal(5_000_000)
        ),
    )
    assert sorted(r.subject for r in screened) == ["000858.SZ", "600519.SH"]
    margins = await world.runs.query_metric(
        "gross_margin", FactQuery(period_from=Y2025), subject=ALICE
    )
    by_subject = {m.subject: m.value for m in margins}
    assert by_subject["600519.SH"] == Decimal("0.8")
    assert by_subject["000568.SZ"] == Decimal("0.75")
    growth = await world.runs.query_metric(
        "revenue_growth",
        FactQuery(subjects=("000858.SZ",), period_from=Y2025),
        subject=ALICE,
    )
    assert [(g.period_end, g.value.quantize(Decimal("0.0001"))) for g in growth] == [
        (Y2025, Decimal("0.1250"))
    ]
    assert {i.period_end for i in growth[0].inputs} == {Y2025, Y2024}
    await refused(
        world.runs.query_market(
            ALICE, FactQuery(codes=("IS.REVENUE", "IS.COST"), order="value_desc")
        ),
        422,
        "order_invalid",
    )
    await refused(
        world.runs.query_market(ALICE, FactQuery(codes=("IS.NOPE",))),
        422,
        "code_invalid",
    )


async def test_run_scope_reads_only_what_the_grant_pinned(world):
    maotai, v_maotai, table = await world.publish("600519.SH", 1500, 300, 1300)
    await world.publish("000858.SZ", 900, 400, 800)
    pin = InputPin(
        maotai.id,
        v_maotai.version_no,
        v_maotai.sha256,
        rendition_kind="table",
        rendition_no=1,
        rendition_sha256=table.sha256,
    )
    await world.runs.grant_by_user(
        ALICE,
        "knowbench",
        "delegation",
        "task-1",
        [GrantRequestItem(pin, "public", PUBLIC_OWNER)],
    )
    rows = await world.runs.query_facts(
        "knowbench", "runner", ALICE, "task-1", FactQuery(codes=("IS.REVENUE",))
    )
    assert {r.subject for r in rows} == {"600519.SH"}
    assert (
        await world.runs.query_facts(
            "knowbench", "runner", BOB, "task-1", FactQuery(codes=("IS.REVENUE",))
        )
        == []
    )
    margin = await world.runs.query_metric(
        "gross_margin",
        FactQuery(),
        executor_app="knowbench",
        reader="runner",
        beneficiary=ALICE,
        run_ref="task-1",
    )
    assert [m.value for m in margin if m.period_end == Y2025] == [Decimal("0.8")]


async def test_market_queries_follow_the_switch(db):
    closed = World(db, gate=PublicGate(enabled=False))
    await refused(
        closed.runs.query_market(ALICE, FactQuery(codes=("IS.REVENUE",))),
        403,
        "public_access_closed",
    )
    await refused(
        closed.runs.query_metric("gross_margin", FactQuery(), subject=ALICE),
        403,
        "public_access_closed",
    )


async def test_market_queries_need_the_qualification_when_required(db):
    """跨全市场的筛选、排名是资格问题（决定 ⑪）：要资格时只认登记的用户。"""
    world = World(
        db, gate=PublicGate(qualification="listed", qualified=frozenset({ALICE}))
    )
    await world.publish("600519.SH", 1500, 300, 1300)
    assert await world.runs.query_market(ALICE, FactQuery(codes=("IS.REVENUE",)))
    await refused(
        world.runs.query_market(BOB, FactQuery(codes=("IS.REVENUE",))),
        403,
        "qualification_required",
    )
    await refused(
        world.runs.query_metric("gross_margin", FactQuery(), subject=BOB),
        403,
        "qualification_required",
    )


async def test_bad_batches_are_refused(world):
    material, version, _ = await world.lib.deliver_public(
        "knowfeed", "cninfo", "ar-x", "x.csv", CSV, report("600519.SH")
    )
    grant = await world.runs.issue_processing(
        "knowfeed", PUBLIC_OWNER, material.id, version.version_no
    )
    raw, standard = statements(1, 1, 1)
    bad_code = [
        StandardFact("IS.NOPE", Decimal(1), Y2025, "year", "consolidated", (0,), "r")
    ]
    await refused(
        world.runs.write_facts("knowfeed", grant.id, "v", raw, bad_code),
        422,
        "code_invalid",
    )
    outside = [
        StandardFact(
            "IS.REVENUE", Decimal(1), Y2025, "year", "consolidated", (99,), "r"
        )
    ]
    await refused(
        world.runs.write_facts("knowfeed", grant.id, "v", raw, outside),
        422,
        "derived_from_invalid",
    )
    await refused(
        world.runs.write_facts("knowfeed", grant.id, "v", raw, standard + standard[:1]),
        422,
        "facts_duplicate",
    )
    bad_unit = [RawFact("营业收入", Decimal(1), "美元")]
    await refused(
        world.runs.write_facts("knowfeed", grant.id, "v", bad_unit, []),
        422,
        "unit_invalid",
    )
    await refused(
        world.runs.write_facts("knowbench", grant.id, "v", raw, standard),
        403,
        "service_scope_denied",
    )


async def test_write_and_query_facts_from_structured_wind_feed(world):
    """WP-6: 将 Wind 结构化报表数据转为事实表记录并持久化，支持精确事实查询。"""
    material, version, _ = await world.lib.deliver_public(
        "knowfeed",
        "wind",
        "wind:600519:2024",
        "maotai_2024.csv",
        CSV,
        report("600519.SH"),
    )
    grant = await world.runs.issue_processing(
        "knowfeed", PUBLIC_OWNER, material.id, version.version_no
    )

    feed_payload = {
        "source": "wind",
        "subject": "600519.SH",
        "doc_type": "annual_report",
        "report_date": "2025-12-31",
        "facts": {
            "IS.REVENUE": "170987192572.76",
            "IS.COST": "14142389864.71",
            "CF.NET_OPERATING": "92442166423.51",
            "CF.CAPEX": "5671234180.20",
        },
    }
    from app.domain.user_facts import from_structured_feed

    raw, standard = from_structured_feed(feed_payload)
    assert len(raw) == 4
    assert len(standard) == 4

    rendition = await world.runs.write_facts(
        "knowfeed", grant.id, "processor-v1", raw, standard
    )
    assert rendition.status == "ready"

    # 查全市场事实
    query = FactQuery(
        layer="standard",
        codes=("IS.REVENUE", "CF.NET_OPERATING"),
        subjects=("600519.SH",),
    )
    rows = await world.runs.query_market(ALICE, query)
    assert len(rows) == 2
    fact_map = {r.code: r.value for r in rows}
    assert fact_map["IS.REVENUE"] == Decimal("170987192572.76")
    assert fact_map["CF.NET_OPERATING"] == Decimal("92442166423.51")
