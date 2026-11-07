"""要哪些数的解析（SDD 0051 第 2.2 节）。

在他能用的资料的表格读法里找，按披露版本挑，缺的交 knowfeed。
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest
from test_durable_delivery_db import db as db  # noqa: F401
from test_user_resolution_db import ALICE, MAOTAI, World

from app.domain.fact_needs import FactKey, FactNeed
from app.domain.user_facts import RawFact, StandardFact
from app.domain.user_resolution import CONTRACT_VERSION

REVENUE = "IS.REVENUE"


def need(**kw) -> FactNeed:
    base = {
        "key": "revenue",
        "name": "营业收入",
        "subject": MAOTAI,
        "start": date(2023, 1, 1),
        "end": date(2025, 12, 31),
        "codes": (REVENUE,),
    }
    return FactNeed(**{**base, **kw})


def facts(values: dict[int, int]) -> tuple[list[RawFact], list[StandardFact]]:
    """一份年报表格读法里的营业收入：{年份: 数}（本期与上年同期）。"""
    raw, standard = [], []
    for index, (year, value) in enumerate(sorted(values.items())):
        end = date(year, 12, 31)
        raw.append(
            RawFact(
                label="营业收入",
                value=Decimal(value),
                unit="元",
                period_end=end,
                duration="year",
                consolidation="consolidated",
                page=10,
            )
        )
        standard.append(
            StandardFact(
                code=REVENUE,
                value=Decimal(value),
                period_end=end,
                duration="year",
                consolidation="consolidated",
                derived_from=(index,),
                rule="label:营业收入",
                page=10,
            )
        )
    return raw, standard


async def report_with_facts(world: World, year: int, values, *, show=True):
    material, version = await world.report(ALICE, year, show=show)
    await world.lib.consent_processing(ALICE, material.id, version.version_no)
    grant = await world.runs.issue_processing(
        "knowfeed", ALICE, material.id, version.version_no
    )
    raw, standard = facts(values)
    await world.runs.write_facts("knowfeed", grant.id, "ar-0.1", raw, standard)
    return material


@pytest.fixture
def world(db) -> World:
    return World(db)


def by_year(resolution):
    return {i.key.period_end.year: i for i in resolution.items}


async def test_first_disclosure_and_latest_pick_different_reports(world):
    """2023 年的营业收入：2023 年报里 100，2024 年报的上年同期改成 98。"""
    ar23 = await report_with_facts(world, 2023, {2023: 100, 2022: 90})
    ar24 = await report_with_facts(world, 2024, {2024: 120, 2023: 98})
    first = by_year(await world.resolve([need()]))
    assert first[2023].status == "resolved"
    assert [c.material_id for c in first[2023].chosen] == [ar23.id]
    assert first[2023].chosen[0].rendition_kind == "table"
    assert [c.material_id for c in first[2024].chosen] == [ar24.id]
    latest = by_year(await world.resolve([need(disclosure="latest")]))
    assert [c.material_id for c in latest[2023].chosen] == [ar24.id]


async def test_missing_periods_go_to_knowfeed_and_the_key_carries_no_channel(world):
    await report_with_facts(world, 2024, {2024: 120, 2023: 98})
    result = await world.resolve([need()])
    item = by_year(result)[2025]
    assert (item.status, item.reason) == ("to_confirm", "facts_missing")
    key = item.key.as_dict()
    assert key == {
        "kind": "facts",
        "subject": MAOTAI,
        "period_end": "2025-12-31",
        "codes": [REVENUE],
        "consolidation": "consolidated",
        "duration": "year",
        "disclosure": "first",
    }
    confirmation, branches = await world.resolver.confirm(
        ALICE,
        "knowbench",
        "description",
        [{"key": key, "need_key": "revenue"}],
        {},
        resolution_id=result.id,
    )
    assert branches == [{"item_id": "c1", "branch": "supply"}]
    assert confirmation.items[0]["reading"] == "table"


async def test_a_report_not_shown_to_the_app_is_not_used(world):
    await report_with_facts(world, 2025, {2025: 130}, show=False)
    item = by_year(await world.resolve([need(start=date(2025, 1, 1))]))[2025]
    assert item.status == "not_shown"


async def test_the_stored_list_reads_back_with_facts_keys(world):
    await report_with_facts(world, 2024, {2024: 120, 2023: 98})
    result = await world.resolve([need()])
    stored = await world.resolver.repo.resolution(result.id, result.revision)
    assert stored is not None
    assert all(isinstance(i.key, FactKey) for i in stored.items)
    assert isinstance(stored.needs[0], FactNeed)
    assert stored.contract_version == CONTRACT_VERSION
    assert [i.status for i in stored.items] == [i.status for i in result.items]
