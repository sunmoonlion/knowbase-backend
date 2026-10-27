"""按描述查、解析、确认、偏好（数据第四版，SDD 0043 第三节，块 2）。

展开规则是纯函数，单测；其余走真数据库。
"""

from __future__ import annotations

from datetime import date, timedelta

import pytest
from test_durable_delivery_db import db as db
from test_user_data import CSV, Clock, MemoryObjects

from app.application.services.call_rate import RateLimiter
from app.application.services.user_library_service import UserLibraryService
from app.application.services.user_resolution_service import UserResolutionService
from app.application.services.user_run_service import UserRunService
from app.domain.user_data import UserDataError
from app.domain.user_library import Description
from app.domain.user_resolution import (
    CONTRACT_VERSION,
    DescriptionKey,
    NeedSpec,
    PeriodRule,
    TaskParams,
    expand,
)
from app.infrastructure.repositories.user_library import SqlUserLibraryRepository
from app.infrastructure.repositories.user_resolution import (
    SqlUserResolutionRepository,
)
from app.infrastructure.repositories.user_runs import SqlUserRunRepository

ALICE = "alice"
BOB = "bob"
MAOTAI = "600519.SH"
TASK_DAY = date(2027, 3, 15)


def annual(count=3, **kw) -> NeedSpec:
    return NeedSpec(
        key="annual",
        name="年报",
        doc_type="annual_report",
        period=PeriodRule("latest", count=count, disclosed_by_task_date=True),
        formats=("csv", "pdf"),
        **kw,
    )


PARAMS = TaskParams(subjects=(MAOTAI,), task_date=TASK_DAY)


def test_latest_periods_hold_back_the_one_not_yet_due():
    """2027-03-15 要最近 2 年、截至任务日期已披露：2026 年报法定 4-30 前披露，
    先标 tentative，另展开 2025、2024 两期备着。"""
    wanted = expand(annual(count=2), PARAMS)
    assert [(w.key.period_end, w.tentative) for w in wanted] == [
        (date(2026, 12, 31), True),
        (date(2025, 12, 31), False),
        (date(2024, 12, 31), False),
    ]
    assert wanted[1].key.period_start == date(2025, 1, 1)
    plain = expand(
        NeedSpec(
            **{**annual(count=2).__dict__, "period": PeriodRule("latest", count=2)}
        ),
        PARAMS,
    )
    assert [w.key.period_end.year for w in plain] == [2026, 2025]
    half = NeedSpec(
        key="half",
        name="半年报",
        doc_type="semiannual_report",
        period=PeriodRule("fixed", date(2024, 1, 1), date(2026, 12, 31)),
        formats=("pdf",),
    )
    assert [w.key.period_end for w in expand(half, PARAMS)] == [
        date(2026, 6, 30),
        date(2025, 6, 30),
        date(2024, 6, 30),
    ]


def test_tables_are_for_tabular_data_only():
    with pytest.raises(UserDataError) as caught:
        annual(tables=((None, ("营业收入",)),)).checked()
    assert caught.value.code == "tables_invalid"
    ok = NeedSpec(
        **{
            **annual().__dict__,
            "formats": ("excel",),
            "tables": (("利润表", ("营业收入",)),),
        }
    ).checked()
    assert ok.as_dict()["tables"] == [{"sheet": "利润表", "columns": ["营业收入"]}]


class World:
    def __init__(self, db) -> None:
        self.clock = Clock()
        self.clock.now = self.clock.now.replace(year=2027, month=3, day=15)
        objects = MemoryObjects()
        library = SqlUserLibraryRepository(lambda: db)
        runs = SqlUserRunRepository(lambda: db)
        self.lib = UserLibraryService(
            library, objects, prefix="user-data", clock=self.clock
        )
        self.runs = UserRunService(
            library, runs, objects, prefix="user-data", clock=self.clock
        )
        self.resolver = UserResolutionService(
            SqlUserResolutionRepository(lambda: db),
            library,
            runs,
            clock=self.clock,
            limiter=RateLimiter(1000),
        )

    async def report(
        self, subject, year, *, show=True, disclosed=None, name=None, origin_csv=CSV
    ):
        material, version = await self.lib.upload(
            subject,
            name or f"maotai-{year}.csv",
            origin_csv + str(year).encode(),
            description=Description(
                subject=MAOTAI,
                doc_type="annual_report",
                period_start=date(year, 1, 1),
                period_end=date(year, 12, 31),
                disclosed_on=disclosed or date(year + 1, 4, 1),
            ),
            confirm=True,
        )
        if show:
            await self.lib.show(subject, material.id, "app:knowbench")
        return material, version

    async def resolve(self, needs, params=PARAMS, consumer="expert:fa-1@2"):
        return await self.resolver.resolve(
            ALICE, "knowbench", consumer, CONTRACT_VERSION, needs, params
        )


@pytest.fixture
def world(db) -> World:
    return World(db)


async def refused(work, status: int, code: str) -> None:
    with pytest.raises(UserDataError) as caught:
        await work
    assert (caught.value.status, caught.value.code) == (status, code)


def statuses(resolution):
    return [(i.key.period_end.year, i.status) for i in resolution.items]


async def test_resolve_marks_each_period(world):
    y2025, _ = await world.report(ALICE, 2025)
    await world.report(ALICE, 2024, show=False)
    first = await world.resolve([annual()])
    # 2026 还没到截止、他也没有：不列；2025 已定；2024 没放开；2023 待确认
    assert statuses(first) == [
        (2025, "resolved"),
        (2024, "not_shown"),
        (2023, "to_confirm"),
    ]
    assert first.items[0].chosen[0].material_id == y2025.id
    assert first.revision == 1

    # 2026 年报 3-10 就披露了、他也有：用它，去掉最老的一期
    await world.report(ALICE, 2026, disclosed=date(2027, 3, 10))
    second = await world.resolve([annual()])
    assert statuses(second) == [
        (2026, "resolved"),
        (2025, "resolved"),
        (2024, "not_shown"),
    ]
    # 披露在任务日期之后的不算
    late = await world.resolve(
        [annual()], TaskParams(subjects=(MAOTAI,), task_date=date(2027, 3, 5))
    )
    assert statuses(late)[0] == (2025, "resolved")


async def test_private_types_cannot_be_collected_and_disabled_cannot_be_used(world):
    table = NeedSpec(
        key="ledger",
        name="内部台账",
        doc_type="other_table",
        period=PeriodRule("none"),
        formats=("csv",),
    )
    result = await world.resolve([table])
    assert [(i.status, i.reason) for i in result.items] == [
        ("not_collectable", "private")
    ]
    material, _ = await world.report(ALICE, 2025)
    await world.runs.disable("admin", material.id, "违规")
    result = await world.resolve([annual(count=1)])
    assert [(i.status, i.reason) for i in result.items] == [("unusable", "disabled")]


async def test_several_candidates_need_a_choice_priority_or_preference(world):
    old, _ = await world.report(ALICE, 2025, disclosed=date(2026, 3, 1), name="a.csv")
    new, _ = await world.report(
        ALICE, 2025, disclosed=date(2026, 3, 20), name="b.csv", origin_csv=CSV + b"x"
    )
    result = await world.resolve([annual(count=1)])
    assert result.items[0].status == "ambiguous"
    assert {c.material_id for c in result.items[0].candidates} == {old.id, new.id}
    newest = await world.resolve([annual(count=1, priority="newest")])
    assert newest.items[0].chosen[0].material_id == new.id
    await world.resolver.record_preference(
        ALICE, "expert:fa-1@2", "annual", PARAMS, [old.id]
    )
    preferred = await world.resolve([annual(count=1)])
    assert (preferred.items[0].status, preferred.items[0].reason) == (
        "resolved",
        "preference",
    )
    assert preferred.items[0].chosen[0].material_id == old.id
    # 偏好只认同一个使用者
    other = await world.resolve([annual(count=1)], consumer="expert:fa-2@1")
    assert other.items[0].status == "ambiguous"
    prefs = await world.resolver.preferences(ALICE, "expert:fa-1@2", PARAMS)
    assert [(p.need_key, p.material_ids) for p in prefs] == [("annual", (old.id,))]


async def test_reading_must_be_ready(world):
    material, version = await world.report(ALICE, 2025)
    need = annual(count=1, reading="text")
    result = await world.resolve([need])
    assert (result.items[0].status, result.items[0].reason) == (
        "to_confirm",
        "reading_missing",
    )
    await world.lib.consent_processing(ALICE, material.id, 1)
    grant = await world.runs.issue_processing("knowfeed", ALICE, material.id, 1)
    await world.runs.write_rendition("knowfeed", grant.id, "text", "0.1", b"text")
    result = await world.resolve([need])
    chosen = result.items[0].chosen[0]
    assert (result.items[0].status, chosen.rendition_kind, chosen.rendition_no) == (
        "resolved",
        "text",
        1,
    )


async def test_confirm_only_what_waits_then_resolution_waits(world):
    await world.report(ALICE, 2025)
    result = await world.resolve([annual(count=3)])
    waiting = next(i for i in result.items if i.status == "to_confirm")
    resolved = next(i for i in result.items if i.status == "resolved")
    await refused(
        world.resolver.confirm(
            ALICE,
            "knowbench",
            "description",
            [{"key": resolved.key.as_dict(), "need_key": "annual"}],
            {},
            resolution_id=result.id,
        ),
        403,
        "outside_resolution",
    )
    confirmation, branches = await world.resolver.confirm(
        ALICE,
        "knowbench",
        "description",
        [{"key": waiting.key.as_dict(), "need_key": "annual"}],
        {"prematch": [{"item": "c1", "channel": "cninfo", "eta_hours": 2}]},
        task_ref="task-1",
        resolution_id=result.id,
    )
    assert branches == [{"item_id": "c1", "branch": "supply"}]
    assert confirmation.expires_at - confirmation.created_at == timedelta(days=7)
    assert confirmation.needs[0]["key"] == "annual"
    again = await world.resolve([annual(count=3)])
    assert [i.status for i in again.items if i.key == waiting.key] == ["waiting"]

    checked = await world.resolver.check_confirmation(
        "knowfeed", confirmation.id, ALICE
    )
    assert checked.items[0]["key"] == waiting.key.as_dict()
    await refused(
        world.resolver.check_confirmation("knowfeed", confirmation.id, BOB),
        404,
        "confirmation_not_found",
    )
    await refused(
        world.resolver.check_confirmation("knowbench", confirmation.id, ALICE),
        403,
        "service_scope_denied",
    )
    world.clock.now += timedelta(days=8)
    await refused(
        world.resolver.check_confirmation("knowfeed", confirmation.id, ALICE),
        403,
        "confirmation_expired",
    )


async def test_confirm_without_a_resolution_urls_and_ready_branch(world):
    await world.report(ALICE, 2025)
    have = DescriptionKey(MAOTAI, "annual_report", date(2025, 1, 1), date(2025, 12, 31))
    lack = DescriptionKey(MAOTAI, "annual_report", date(2022, 1, 1), date(2022, 12, 31))
    _, branches = await world.resolver.confirm(
        ALICE,
        "knowbench",
        "description",
        [{"key": have.as_dict()}, {"key": lack.as_dict()}],
        {},
    )
    assert [b["branch"] for b in branches] == ["ready", "supply"]
    url, _ = await world.resolver.confirm(
        ALICE, "knowlab", "url", [{"url": "https://www.pbc.gov.cn/a.html"}], {}
    )
    assert url.items[0]["url"] == "https://www.pbc.gov.cn/a.html"
    await refused(
        world.resolver.confirm(ALICE, "knowlab", "url", [{"url": "ftp://x"}], {}),
        422,
        "url_invalid",
    )
    await refused(
        world.resolver.confirm(ALICE, "knowmart", "url", [{"url": "https://a"}], {}),
        422,
        "app_invalid",
    )


async def test_new_needs_make_a_revision_and_keep_the_rest(world):
    await world.report(ALICE, 2025)
    first = await world.resolve([annual(count=1)])
    policy = NeedSpec(
        key="policy",
        name="政策",
        doc_type="policy",
        period=PeriodRule("none"),
        formats=("pdf",),
        required=False,
    )
    await refused(
        world.resolver.add_needs(ALICE, "knowbench", first.id, [annual(count=1)]),
        422,
        "need_key_taken",
    )
    second = await world.resolver.add_needs(ALICE, "knowbench", first.id, [policy])
    assert (second.id, second.revision) == (first.id, 2)
    assert second.items[0] == first.items[0]
    # 上市公司的政策文件不按描述采
    assert (second.items[1].need_key, second.items[1].status) == (
        "policy",
        "not_collectable",
    )
    old = await world.resolver.get_resolution(ALICE, "knowbench", first.id, 1)
    assert len(old.items) == 1
    await refused(
        world.resolver.get_resolution(BOB, "knowbench", first.id, None),
        404,
        "resolution_not_found",
    )


async def test_lookup_answers_in_or_out_and_is_rate_limited(world):
    key = DescriptionKey(MAOTAI, "annual_report", date(2025, 1, 1), date(2025, 12, 31))
    assert await world.resolver.lookup(ALICE, "knowbench", key) == ("absent", None)
    material, _ = await world.report(ALICE, 2025, show=False)
    assert await world.resolver.lookup(ALICE, "knowbench", key) == ("held", "own")
    await world.lib.show(ALICE, material.id, "app:knowbench")
    assert await world.resolver.lookup(ALICE, "knowbench", key) == ("shown", "own")
    assert await world.resolver.lookup(BOB, "knowbench", key) == ("absent", None)
    world.resolver.limiter = RateLimiter(1)
    await world.resolver.lookup(ALICE, "knowbench", key)
    await refused(
        world.resolver.lookup(ALICE, "knowbench", key), 429, "too_many_lookups"
    )
    await refused(
        world.resolver.lookup(
            BOB, "knowbench", DescriptionKey("茅台", "annual_report")
        ),
        422,
        "subject_invalid",
    )
