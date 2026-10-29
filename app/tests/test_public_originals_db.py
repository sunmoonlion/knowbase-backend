"""公开原件存成资料（SDD 0047 第二节，第 2 步 2b）。

只存一份、引用、公开可读、立即完成。
"""

from __future__ import annotations

from datetime import date

import pytest
from test_durable_delivery_db import db as db
from test_user_data import CSV, Clock, MemoryObjects

from app.application.services.call_rate import RateLimiter
from app.application.services.user_library_service import UserLibraryService
from app.application.services.user_resolution_service import UserResolutionService
from app.application.services.user_run_service import UserRunService
from app.domain.public_access import PublicGate
from app.domain.user_data import UserDataError
from app.domain.user_library import PUBLIC_OWNER, Description
from app.domain.user_resolution import (
    CONTRACT_VERSION,
    DescriptionKey,
    NeedSpec,
    PeriodRule,
    TaskParams,
)
from app.domain.user_runs import GrantRequestItem, InputPin
from app.infrastructure.repositories.user_library import SqlUserLibraryRepository
from app.infrastructure.repositories.user_resolution import (
    SqlUserResolutionRepository,
)
from app.infrastructure.repositories.user_runs import SqlUserRunRepository

ALICE = "alice"
BOB = "bob"
MAOTAI = "600519.SH"
REPORT_2025 = Description(
    subject=MAOTAI,
    doc_type="annual_report",
    period_start=date(2025, 1, 1),
    period_end=date(2025, 12, 31),
    disclosed_on=date(2026, 3, 28),
)
KEY_2025 = DescriptionKey(MAOTAI, "annual_report", date(2025, 1, 1), date(2025, 12, 31))
NEED = NeedSpec(
    key="annual",
    name="年报",
    doc_type="annual_report",
    period=PeriodRule("fixed", date(2025, 1, 1), date(2025, 12, 31)),
    formats=("csv",),
    subject=MAOTAI,
)


class World:
    def __init__(self, db, *, gate: PublicGate) -> None:
        clock = Clock()
        objects = MemoryObjects()
        library = SqlUserLibraryRepository(lambda: db)
        runs = SqlUserRunRepository(lambda: db)
        self.lib = UserLibraryService(library, objects, prefix="p", clock=clock)
        self.runs = UserRunService(
            library, runs, objects, prefix="p", clock=clock, gate=gate
        )
        self.resolver = UserResolutionService(
            SqlUserResolutionRepository(lambda: db),
            library,
            runs,
            clock=clock,
            limiter=RateLimiter(1000),
            gate=gate,
        )

    async def deliver(self, content=CSV, references=None, doc="cninfo:1219876543"):
        return await self.lib.deliver_public(
            "knowfeed",
            "cninfo",
            doc,
            "贵州茅台2025年年度报告.csv",
            content,
            REPORT_2025,
            source_url="https://www.cninfo.com.cn/x",
            references=references,
        )

    async def resolve(self, subject=ALICE):
        return await self.resolver.resolve(
            subject,
            "knowbench",
            "expert:fa-1@2",
            CONTRACT_VERSION,
            [NEED],
            TaskParams(task_date=date(2026, 10, 27)),
        )


@pytest.fixture
def open_world(db) -> World:
    return World(db, gate=PublicGate())


@pytest.fixture
def closed_world(db) -> World:
    return World(db, gate=PublicGate(enabled=False))


async def refused(work, status: int, code: str) -> None:
    with pytest.raises(UserDataError) as caught:
        await work
    assert (caught.value.status, caught.value.code) == (status, code)


def ask(version, basis, ref) -> list[GrantRequestItem]:
    return [
        GrantRequestItem(
            InputPin(version.material_id, version.version_no, version.sha256),
            basis,
            ref,
        )
    ]


async def test_one_copy_per_channel_document_and_corrections_are_versions(open_world):
    w = open_world
    material, v1, refs = await w.deliver(references=[(ALICE, "basis-1")])
    assert (material.owner, material.content_origin, material.license) == (
        PUBLIC_OWNER,
        "disclosure",
        "via_expert",
    )
    assert material.description.status == "checked"
    assert [(h.subject, h.basis, h.ref) for h in refs] == [
        (ALICE, "reference", "basis-1")
    ]
    again, same, _ = await w.deliver(references=[(BOB, "basis-2")])
    assert (again.id, same.version_no) == (material.id, 1)
    corrected, v2, _ = await w.deliver(content=CSV + b"9,9\n")
    assert (corrected.id, v2.version_no) == (material.id, 2)
    # 引用在他名下的「为我采集的」，到交来的那一版
    entry = await w.lib.get(ALICE, material.id)
    assert (entry.category, entry.version.version_no) == ("collected", 1)
    await refused(
        w.lib.deliver_public("knowmart", "cninfo", "x", "a.csv", CSV, REPORT_2025),
        403,
        "service_scope_denied",
    )
    await refused(
        w.lib.add_public_references("knowfeed", material.id, 9, [(BOB, "r")]),
        404,
        "version_not_found",
    )


async def test_open_access_reads_public_originals_without_a_reference(open_world):
    w = open_world
    material, v1, _ = await w.deliver()
    grant = await w.runs.grant_by_user(
        BOB, "knowbench", "delegation", "t-1", ask(v1, "public", PUBLIC_OWNER)
    )
    got = await w.runs.read("knowbench", "r", grant.id, material.id, None, "content")
    assert got.content == CSV
    await refused(
        w.runs.grant_by_user(
            BOB, "knowbench", "delegation", "t-2", ask(v1, "reference", "reference")
        ),
        403,
        "no_reference",
    )
    result = await w.resolve(BOB)
    chosen = result.items[0].chosen[0]
    assert (result.items[0].status, chosen.basis) == ("resolved", "public")
    assert await w.resolver.lookup(BOB, "knowbench", KEY_2025) == ("shown", "public")
    session = await w.runs.register_session(BOB, "knowbench")
    small, _, _ = await w.runs.session_read(BOB, "knowbench", session.id, material.id)
    assert small.items[0].basis == "public"


async def test_closed_access_goes_through_references_and_confirmation(closed_world):
    w = closed_world
    material, v1, _ = await w.deliver(references=[(ALICE, "basis-1")])
    await refused(
        w.runs.grant_by_user(
            BOB, "knowbench", "delegation", "t-1", ask(v1, "public", PUBLIC_OWNER)
        ),
        403,
        "public_access_closed",
    )
    # 有引用的照样能读
    grant = await w.runs.grant_by_user(
        ALICE, "knowbench", "delegation", "t-2", ask(v1, "reference", "reference")
    )
    assert grant.items[0].basis == "reference"
    assert (await w.resolve(ALICE)).items[0].chosen[0].basis == "reference"
    session = await w.runs.register_session(BOB, "knowbench")
    await refused(
        w.runs.session_read(BOB, "knowbench", session.id, material.id),
        403,
        "public_access_closed",
    )

    # 没引用的：解析成待确认；确认时公开原件已齐，当场记引用、立即完成
    result = await w.resolve(BOB)
    assert result.items[0].status == "to_confirm"
    confirmation, branches = await w.resolver.confirm(
        BOB,
        "knowbench",
        "description",
        [{"key": KEY_2025.as_dict(), "need_key": "annual"}],
        {},
        resolution_id=result.id,
    )
    assert branches == [{"item_id": "c1", "branch": "ready"}]
    entry = await w.lib.get(BOB, material.id)
    assert entry.holding.ref == f"confirmation:{confirmation.id}:c1"
    after = await w.resolve(BOB)
    assert (after.items[0].status, after.items[0].chosen[0].basis) == (
        "resolved",
        "reference",
    )


async def test_qualification_is_part_of_the_one_gate(db):
    """资格、权限、平台总开关同一道门（0047 第二、四、五节，决定 ⑪）：要资格时只认登记的
    用户；签授权、会话首读、解析候选、按描述查走的是同一个门。引用不过这道门。"""
    w = World(db, gate=PublicGate(qualification="listed", qualified=frozenset({BOB})))
    material, v1, _ = await w.deliver(references=[(ALICE, "basis-1")])
    carol = "carol"
    await refused(
        w.runs.grant_by_user(
            carol, "knowbench", "delegation", "t-1", ask(v1, "public", PUBLIC_OWNER)
        ),
        403,
        "qualification_required",
    )
    session = await w.runs.register_session(carol, "knowbench")
    await refused(
        w.runs.session_read(carol, "knowbench", session.id, material.id),
        403,
        "qualification_required",
    )
    assert (await w.resolve(carol)).items[0].status == "to_confirm"
    assert await w.resolver.lookup(carol, "knowbench", KEY_2025) == ("absent", None)
    # 登记了资格的照常
    grant = await w.runs.grant_by_user(
        BOB, "knowbench", "delegation", "t-2", ask(v1, "public", PUBLIC_OWNER)
    )
    assert grant.items[0].basis == "public"
    assert (await w.resolve(BOB)).items[0].chosen[0].basis == "public"
    # 名下有引用的不看资格
    held = await w.runs.grant_by_user(
        ALICE, "knowbench", "delegation", "t-3", ask(v1, "reference", "reference")
    )
    assert held.items[0].basis == "reference"


async def test_the_gate_only_lets_readable_public_originals_through(open_world):
    """权限：门上核这一份是公开原件、没禁用（0047 第 5.2 节「谁能看」）。"""
    w = open_world
    material, v1, _ = await w.deliver()
    await w.runs.disable("admin-1", material.id, "违规")
    gate = PublicGate()
    gate.check(BOB)
    with pytest.raises(UserDataError) as caught:
        gate.check(BOB, await w.lib.repo.get_material(material.id))
    assert (caught.value.status, caught.value.code) == (403, "not_public")
