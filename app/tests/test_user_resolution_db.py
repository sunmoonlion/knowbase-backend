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
        name="法律",
        doc_type="law",
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
    # 法律不按上市公司的主体采（政策法规大类里只有监管函件能按证券代码采）
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


async def test_the_runtime_adds_needs_but_cannot_confirm(world):
    """0048 第五节 `request_data`：执行的应用替运行中的专家提新需求，只出修订。"""
    await world.report(ALICE, 2025)
    first = await world.resolve([annual(count=1)])
    extra = NeedSpec(
        **{
            **annual(count=1).__dict__,
            "key": "older",
            "period": PeriodRule("fixed", date(2022, 1, 1), date(2022, 12, 31)),
        }
    )
    await refused(
        world.resolver.add_needs_for_run("knowlab", ALICE, first.id, [extra]),
        404,
        "resolution_not_found",
    )
    second = await world.resolver.add_needs_for_run(
        "knowbench", ALICE, first.id, [extra]
    )
    assert second.revision == 2
    assert [(i.need_key, i.status) for i in second.items] == [
        ("annual", "resolved"),
        ("older", "to_confirm"),
    ]


async def test_data_arrives_refresh_then_grant_by_the_confirmation(world):
    """所有者 10-27 定：等数据的委托数据齐了自动接着跑——执行的应用重解析清单，
    凭本人那次确认续签；签进去的只能是清单里的候选。"""
    from app.domain.user_runs import GrantRequestItem, InputPin

    have, have_version = await world.report(ALICE, 2025)
    first = await world.resolve([annual(count=2)])
    lacking = next(i for i in first.items if i.status == "to_confirm")
    confirmation, _ = await world.resolver.confirm(
        ALICE,
        "knowbench",
        "description",
        [{"key": lacking.key.as_dict(), "need_key": "annual"}],
        {},
        task_ref="knowbench:k-1",
        resolution_id=first.id,
        revision=first.revision,
    )
    # 确认了还没到：那一期读作「等数据」；再刷一次没变就不出新修订
    waiting = await world.resolver.refresh_for_run("knowbench", ALICE, first.id)
    assert [i.status for i in waiting.items] == ["resolved", "waiting"]
    same = await world.resolver.refresh_for_run("knowbench", ALICE, first.id)
    assert same.revision == waiting.revision == first.revision + 1
    await refused(
        world.resolver.refresh_for_run("knowmart", ALICE, first.id),
        403,
        "service_scope_denied",
    )
    await refused(
        world.resolver.refresh_for_run("knowbench", BOB, first.id),
        404,
        "resolution_not_found",
    )
    # 到了（这里用他自己上传代替 knowfeed 交付）：缺的那期定了，已定的原样
    year = lacking.key.period_end.year
    got, got_version = await world.report(ALICE, year)
    fresh = await world.resolver.refresh_for_run("knowbench", ALICE, first.id)
    assert fresh.revision == waiting.revision + 1
    assert [i.status for i in fresh.items] == ["resolved", "resolved"]
    assert fresh.items[0] == first.items[0]

    async def check(**kw):
        args = {
            "caller": "knowbench",
            "subject": ALICE,
            "confirmation_id": confirmation.id,
            "ref": "knowbench:k-1",
            "resolution_id": first.id,
            "revision": fresh.revision,
            **kw,
        }
        return await world.resolver.confirmed_for_run(**args)

    await refused(check(ref="knowbench:other"), 403, "confirmation_mismatch")
    await refused(check(subject=BOB), 404, "confirmation_not_found")
    await refused(check(caller="knowlab"), 404, "confirmation_not_found")
    resolution = await check()

    def item(material, version, basis="display", ref="app:knowbench"):
        return GrantRequestItem(
            InputPin(material.id, version.version_no, version.sha256), basis, ref
        )

    grant = await world.runs.grant_by_confirmation(
        "knowbench",
        ALICE,
        confirmation.id,
        "knowbench:k-1",
        resolution,
        [item(have, have_version), item(got, got_version)],
    )
    assert (grant.process, grant.beneficiary, grant.issued_by) == (
        "delegation",
        ALICE,
        f"confirmation:{confirmation.id}",
    )
    assert {i.material_id for i in grant.items} == {have.id, got.id}
    # 清单外的一份、别的专家版本的绑定：不签
    other, other_version = await world.report(ALICE, 2019)
    await refused(
        world.runs.grant_by_confirmation(
            "knowbench",
            ALICE,
            confirmation.id,
            "knowbench:k-1",
            resolution,
            [item(other, other_version)],
        ),
        403,
        "outside_resolution",
    )
    await refused(
        world.runs.grant_by_confirmation(
            "knowbench",
            ALICE,
            confirmation.id,
            "knowbench:k-1",
            resolution,
            [item(other, other_version, "frozen_binding", "expert:fa-9@1")],
        ),
        403,
        "binding_mismatch",
    )
    await refused(
        world.runs.grant_by_confirmation(
            "knowlab",
            ALICE,
            confirmation.id,
            "knowbench:k-1",
            resolution,
            [item(have, have_version)],
        ),
        422,
        "app_invalid",
    )
    world.clock.now += timedelta(days=8)
    await refused(check(), 403, "confirmation_expired")


def test_vocabulary_v2_classes_and_dimensions():
    """SDD 0049：大类按发布者、细类是文献类型，编码不重名；大类展开成细类。"""
    from app.domain import vocabulary as v

    assert not set(v.DOC_TYPES) & set(v.DOC_CLASSES)
    assert v.types_of("annual_report") == ("annual_report",)
    assert set(v.types_of("news")) == {
        "financial_news",
        "recommended_news",
        "negative_news",
    }
    assert v.class_of("regulatory_letter") == "policy"
    # 政策法规大类里只有监管函件能按证券代码采；研报、资讯采不来
    assert v.collectable(MAOTAI, "policy") and not v.collectable(MAOTAI, "law")
    assert not v.collectable(MAOTAI, "research") and not v.collectable(MAOTAI, "news")
    assert v.collectable("org:kb-pbc", "law")
    v.check_attributes(
        "research_company",
        {"institution": "中信证券", "forecast": ["initiation"], "market": "cn"},
    )
    for doc_type, attrs in (
        ("interim_announcement", {"institution": "x"}),
        ("research_company", {"forecast": ["buy"]}),
        ("research_company", {"color": "red"}),
    ):
        with pytest.raises(v.VocabularyError):
            v.check_attributes(doc_type, attrs)


async def test_announcements_match_by_disclosure_window_topics_and_class(world):
    """非定期报告按披露日落在窗里匹配；事项缩小范围；声明写大类就是这个发布者的都行。"""

    async def announce(name, disclosed, topics, doc_type="interim_announcement"):
        material, _ = await world.lib.upload(
            ALICE,
            name,
            CSV + name.encode(),
            description=Description(
                subject=MAOTAI,
                doc_type=doc_type,
                disclosed_on=disclosed,
                topics=topics,
            ),
            confirm=True,
        )
        await world.lib.show(ALICE, material.id, "app:knowbench")
        return material

    buyback = await announce("回购.csv", date(2027, 1, 10), ("equity",))
    await announce("分红.csv", date(2026, 12, 1), ("dividend",))
    await announce("老回购.csv", date(2025, 6, 1), ("equity",))
    recent = NeedSpec(
        key="buyback",
        name="近一年回购",
        doc_type="interim_announcement",
        period=PeriodRule("recent", months=12),
        formats=("csv",),
        min_count=1,
        max_count=10,
        topics=("equity",),
    )
    result = await world.resolve([recent])
    item = result.items[0]
    assert (item.status, item.key.period_start, item.key.period_end) == (
        "resolved",
        date(2026, 3, 15),
        TASK_DAY,
    )
    assert [c.material_id for c in item.chosen] == [buyback.id]
    # 大类「公告」：窗里的公告都算（回购、分红两份），老的不算
    whole = NeedSpec(
        **{**recent.__dict__, "key": "all", "doc_type": "disclosure", "topics": ()}
    )
    assert len((await world.resolve([whole])).items[0].chosen) == 2
    # recent 只给非定期报告；定期报告用 latest
    await refused(
        world.resolve(
            [
                NeedSpec(
                    **{**annual().__dict__, "period": PeriodRule("recent", months=3)}
                )
            ]
        ),
        422,
        "period_invalid",
    )


async def test_edition_is_part_of_the_key(world):
    """版本形态进键：要全文的不拿摘要充数。"""
    await world.lib.upload(
        ALICE,
        "maotai-2025-summary.csv",
        CSV + b"summary",
        description=Description(
            subject=MAOTAI,
            doc_type="annual_report",
            period_start=date(2025, 1, 1),
            period_end=date(2025, 12, 31),
            disclosed_on=date(2026, 4, 1),
            edition="summary",
        ),
        confirm=True,
    )
    full = await world.resolve([annual(count=1)])
    assert full.items[0].status == "to_confirm"  # 只有摘要：全文还要去采
    summary = await world.resolve(
        [NeedSpec(**{**annual(count=1).__dict__, "edition": "summary"})]
    )
    assert summary.items[0].status == "not_shown"  # 摘要在、没放开给 knowbench
