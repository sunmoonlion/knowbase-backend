"""运行（数据第四版，SDD 0043 块 1c）：运行授权、读、加工、产出区、禁用与清除。

真数据库。
"""

from __future__ import annotations

import importlib.util
import json
import os
import uuid
from datetime import date, timedelta

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import create_async_engine
from test_durable_delivery_db import ROOT, sql
from test_durable_delivery_db import db as db
from test_user_data import CSV, Clock, MemoryObjects

from app.application.services.user_library_service import UserLibraryService
from app.application.services.user_run_service import UserRunService
from app.domain.user_data import UserDataError
from app.domain.user_readings import Page, TextReading, pages_digest, text_digest
from app.domain.user_runs import GrantRequestItem, InputPin
from app.infrastructure.repositories.user_library import SqlUserLibraryRepository
from app.infrastructure.repositories.user_runs import SqlUserRunRepository
from app.infrastructure.storage.postgres import make_session_factory

ALICE = "alice"
BOB = "bob"
CAROL = "carol"


class World:
    def __init__(self, db) -> None:
        self.db = db
        self.objects = MemoryObjects()
        self.clock = Clock()
        self.lib = UserLibraryService(
            SqlUserLibraryRepository(lambda: db),
            self.objects,
            prefix="user-data",
            clock=self.clock,
        )
        self.runs = UserRunService(
            SqlUserLibraryRepository(lambda: db),
            SqlUserRunRepository(lambda: db),
            self.objects,
            prefix="user-data",
            clock=self.clock,
        )

    async def upload(self, subject, name="a.csv", content=CSV, **kw):
        material, version = await self.lib.upload(subject, name, content, **kw)
        return material, version


@pytest.fixture
def world(db) -> World:
    return World(db)


async def refused(work, status: int, code: str) -> None:
    with pytest.raises(UserDataError) as caught:
        await work
    assert (caught.value.status, caught.value.code) == (status, code)


def item(version, basis, ref, **rendition) -> GrantRequestItem:
    return GrantRequestItem(
        pin=InputPin(
            version.material_id, version.version_no, version.sha256, **rendition
        ),
        basis=basis,
        basis_ref=ref,
    )


async def test_delegation_needs_display_and_append_is_a_new_grant(world):
    material, v1 = await world.upload(ALICE)
    ask = [item(v1, "display", "app:knowbench")]
    await refused(
        world.runs.grant_by_user(ALICE, "knowbench", "delegation", "task-1", ask),
        403,
        "not_shown",
    )
    await world.lib.show(ALICE, material.id, "app:knowbench")
    grant = await world.runs.grant_by_user(
        ALICE, "knowbench", "delegation", "task-1", ask
    )
    assert (grant.revision, grant.scope, grant.items[0].content_owner) == (
        1,
        "content",
        ALICE,
    )
    assert grant.expires_at - grant.issued_at == timedelta(days=7)
    got = await world.runs.read(
        "knowbench", "runner", grant.id, material.id, None, "content"
    )
    assert got.content == CSV

    # 运行中追加：同一件事新签一条，已签的不扩大
    other, w1 = await world.upload(ALICE, "b.csv", CSV + b"9,9\n")
    await world.lib.show(ALICE, other.id, "app:knowbench")
    more = await world.runs.grant_by_user(
        ALICE,
        "knowbench",
        "delegation",
        "task-1",
        [item(w1, "display", "app:knowbench")],
    )
    assert more.revision == 2
    await refused(
        world.runs.read("knowbench", "runner", grant.id, other.id, None, "content"),
        404,
        "item_not_found",
    )
    # 别人的同名事不能借来追加
    theirs, x1 = await world.upload(BOB)
    await world.lib.show(BOB, theirs.id, "app:knowbench")
    await refused(
        world.runs.grant_by_user(
            BOB,
            "knowbench",
            "delegation",
            "task-1",
            [item(x1, "display", "app:knowbench")],
        ),
        409,
        "ref_taken",
    )
    # 不给 knowlab，也不能用错展示依据
    await refused(
        world.runs.read("knowlab", "x", grant.id, material.id, None, "content"),
        404,
        "grant_not_found",
    )
    await refused(
        world.runs.grant_by_user(
            ALICE,
            "knowbench",
            "delegation",
            "task-2",
            [item(v1, "display", "app:knowlab")],
        ),
        422,
        "basis_ref_invalid",
    )
    # 钉的摘要对不上就不签
    stale = GrantRequestItem(
        InputPin(material.id, 1, "0" * 64), "display", "app:knowbench"
    )
    await refused(
        world.runs.grant_by_user(ALICE, "knowbench", "delegation", "task-3", [stale]),
        409,
        "sha256_mismatch",
    )
    reads = await world.runs.reads(ALICE, material.id)
    assert [(r["operation"], r["beneficiary"], r["reader_role"]) for r in reads] == [
        ("content", ALICE, "app")
    ]


async def test_session_signs_one_small_grant_per_first_read(world):
    """0048 第五节：本人登记一次会话，模型首读一份时 knowbase 凭会话范围签小授权。"""
    shown = []
    for n in range(3):
        material, _ = await world.upload(ALICE, f"{n}.csv", CSV + bytes([48 + n]))
        await world.lib.show(ALICE, material.id, "app:knowbench")
        shown.append(material.id)
    hidden, _ = await world.upload(ALICE, "hidden.csv", CSV + b"h")
    await refused(world.runs.register_session(ALICE, "knowlab"), 422, "app_invalid")
    session = await world.runs.register_session(ALICE, "knowbench")
    assert session.expires_at - session.created_at == timedelta(hours=24)

    first, total, hint = await world.runs.session_read(
        ALICE, "knowbench", session.id, shown[0]
    )
    assert (len(first.items), total, hint) == (1, 1, False)
    again, total, _ = await world.runs.session_read(
        ALICE, "knowbench", session.id, shown[0]
    )
    assert again.id == first.id and total == 1  # 读过的不再签
    await world.runs.session_read(ALICE, "knowbench", session.id, shown[1])
    await refused(
        world.runs.session_read(ALICE, "knowbench", session.id, hidden.id),
        403,
        "not_shown",
    )
    await refused(
        world.runs.session_read(BOB, "knowbench", session.id, shown[2]),
        404,
        "session_not_found",
    )
    got = await world.runs.read("knowbench", "m", first.id, shown[0], None, "content")
    assert got.content == CSV + b"0"

    assert await world.runs.end_session(ALICE, "knowbench", session.id) == 2
    await refused(
        world.runs.read("knowbench", "m", first.id, shown[0], None, "content"),
        403,
        "grant_inactive",
    )
    await refused(
        world.runs.session_read(ALICE, "knowbench", session.id, shown[2]),
        403,
        "session_ended",
    )


async def test_session_hints_once_when_reads_pass_fifty(world):
    session = await world.runs.register_session(ALICE, "knowbench")
    hints = []
    for n in range(51):
        material, _ = await world.upload(ALICE, f"m{n}.csv", CSV + str(n).encode())
        await world.lib.show(ALICE, material.id, "app:knowbench")
        _, total, hint = await world.runs.session_read(
            ALICE, "knowbench", session.id, material.id
        )
        hints.append((total, hint))
    assert [t for t, h in hints if h] == [50]


async def test_frozen_binding_reads_the_authors_version_for_the_beneficiary(world):
    material, v1 = await world.upload(
        ALICE, "binding.csv", license_="via_expert", attest=True
    )
    private, p1 = await world.upload(ALICE, "private.csv", CSV + b"3,3\n")
    await world.lib.upsert_expert("fa-1", ALICE, "财务分析与预测", None)
    await world.lib.show(ALICE, material.id, "expert:fa-1")
    await world.lib.show(ALICE, private.id, "expert:fa-1")
    frozen = [u for u, _ in await world.lib.freeze_binding(ALICE, "fa-1", "2")]
    assert sorted(
        (u.material_id, u.version_no, u.scope["license"]) for u in frozen
    ) == (sorted([(material.id, 1, "via_expert"), (private.id, 1, "self")]))
    # 只许本人用的：作者自己请专家能读，别人请不行
    await refused(
        world.runs.grant_by_user(
            BOB,
            "knowbench",
            "delegation",
            "task-8",
            [item(p1, "frozen_binding", "expert:fa-1@2")],
        ),
        403,
        "license_self_only",
    )
    own = await world.runs.grant_by_user(
        ALICE,
        "knowbench",
        "delegation",
        "task-7",
        [item(p1, "frozen_binding", "expert:fa-1@2")],
    )
    assert own.items[0].content_owner == ALICE
    # 作者交付后传新版、取消绑定、改低许可：只影响下一版与新的销售
    await world.lib.add_version(ALICE, material.id, None, CSV + b"7,7\n")
    await world.lib.hide(ALICE, material.id, "expert:fa-1")
    await world.lib.set_license(ALICE, material.id, "self", attest=False)

    grant = await world.runs.grant_by_user(
        BOB,
        "knowbench",
        "delegation",
        "task-9",
        [item(v1, "frozen_binding", "expert:fa-1@2")],
    )
    assert (grant.beneficiary, grant.items[0].content_owner) == (BOB, ALICE)
    got = await world.runs.read("knowbench", "r", grant.id, material.id, 1, "content")
    assert got.content == CSV
    reads = await world.runs.reads(ALICE, material.id)
    assert reads[0]["beneficiary"] == BOB
    v2 = await world.lib.versions(ALICE, material.id)
    await refused(
        world.runs.grant_by_user(
            BOB,
            "knowbench",
            "delegation",
            "task-10",
            [item(v2[1], "frozen_binding", "expert:fa-1@2")],
        ),
        403,
        "not_bound",
    )


async def test_knowlab_test_grants_are_fixed_sets(world):
    material, v1 = await world.upload(ALICE)
    await world.lib.show(ALICE, material.id, "app:knowlab")
    ask = [item(v1, "display", "app:knowlab")]
    await refused(
        world.runs.grant_by_service("knowmart", "test", ALICE, "t-1", ask),
        403,
        "service_scope_denied",
    )
    grant = await world.runs.grant_by_service("knowlab", "test", ALICE, "t-1", ask)
    # 测试由运行方 knowbench 跑、模型经它读；签发方是 knowlab（0048 第六节）
    assert grant.issued_by == "knowlab" and grant.executor_app == "knowbench"
    await refused(
        world.runs.grant_by_service("knowlab", "test", ALICE, "t-1", ask),
        409,
        "append_not_allowed",
    )
    # 孵化只看格式
    incubation = await world.runs.grant_by_user(
        ALICE, "knowlab", "incubation", "i-1", ask
    )
    await refused(
        world.runs.read("knowlab", "x", incubation.id, material.id, None, "content"),
        403,
        "format_only",
    )
    view = await world.runs.read(
        "knowlab", "x", incubation.id, material.id, None, "format"
    )
    assert view.format.header == CSV.split(b"\n")[0].decode()


async def test_platform_grants_follow_the_consent_and_die_with_it(world):
    material, v1 = await world.upload(ALICE)
    await world.lib.add_version(ALICE, material.id, None, CSV + b"1,1\n")
    await world.lib.consent_platform(
        ALICE, "knowlab", "review", "d-1", [(material.id, 1)]
    )
    use = "platform:knowlab:review:d-1"
    operator = await world.runs.grant_by_service(
        "knowlab",
        "platform",
        ALICE,
        "d-1/operator",
        [item(v1, "platform", use)],
        reader_role="operator",
    )
    assert operator.expires_at - operator.issued_at == timedelta(days=30)
    assert operator.platform_use == use
    versions = await world.lib.versions(ALICE, material.id)
    await refused(
        world.runs.grant_by_service(
            "knowlab",
            "platform",
            ALICE,
            "d-1/machine",
            [item(versions[1], "platform", use)],
            reader_role="machine",
        ),
        403,
        "outside_consent",
    )
    await refused(
        world.runs.grant_by_service(
            "knowlab", "platform", ALICE, "d-1/x", [item(v1, "platform", use)]
        ),
        422,
        "reader_role_invalid",
    )
    # 重跑的机器授权还能带测试集里钉住的公开原件、引用（各凭各的，不凭平台同意）；
    # 运营只认平台同意覆盖的那几份
    other, o1 = await world.upload(BOB, "b.csv", CSV + b"9,9\n")
    await world.lib.consent_platform(
        ALICE, "knowlab", "rerun", "d-1", [(material.id, 1)]
    )
    rerun = "platform:knowlab:rerun:d-1"
    await refused(
        world.runs.grant_by_service(
            "knowlab",
            "platform",
            ALICE,
            "d-1/op2",
            [item(v1, "platform", rerun), item(o1, "reference", "reference")],
            reader_role="operator",
        ),
        422,
        "basis_not_allowed",
    )
    await refused(
        world.runs.grant_by_service(
            "knowlab",
            "platform",
            ALICE,
            "d-1/rerun",
            [item(v1, "platform", rerun), item(o1, "reference", "reference")],
            reader_role="machine",
        ),
        403,
        "no_reference",
    )
    machine = await world.runs.grant_by_service(
        "knowlab",
        "platform",
        ALICE,
        "d-1/rerun",
        [item(v1, "platform", rerun)],
        reader_role="machine",
    )
    assert machine.platform_use == rerun
    assert machine.expires_at - machine.issued_at == timedelta(days=14)
    got = await world.runs.read(
        "knowlab", "op-7", operator.id, material.id, 1, "content"
    )
    assert got.content == CSV
    assert await world.runs.withdraw_platform(ALICE, "knowlab", "review", "d-1") == 1
    await refused(
        world.runs.read("knowlab", "op-7", operator.id, material.id, 1, "content"),
        403,
        "grant_inactive",
    )
    reads = await world.runs.reads(ALICE, material.id)
    assert reads[0]["reader_role"] == "operator"


async def test_processing_reads_one_version_and_writes_renditions(world):
    material, v1 = await world.upload(ALICE, "scan.csv", process=True)
    await refused(
        world.runs.pending_processing("knowbench"), 403, "service_scope_denied"
    )
    assert await world.runs.pending_processing("knowfeed") == [(ALICE, material.id, 1)]
    grant = await world.runs.issue_processing("knowfeed", ALICE, material.id, 1)
    assert (grant.process, grant.reader_role, grant.issued_by) == (
        "processing",
        "system",
        "knowbase",
    )
    got = await world.runs.read(
        "knowfeed", "worker", grant.id, material.id, 1, "content"
    )
    assert got.content == CSV
    stored = dict(world.objects.data)
    text = await world.runs.write_rendition(
        "knowfeed", grant.id, "text", "0.1", b"plain text"
    )
    assert (text.rendition_no, text.status) == (1, "ready")
    assert text.sha256 == text_digest(TextReading("plain text"))
    failed = await world.runs.write_rendition(
        "knowfeed", grant.id, "table", "0.1", None, {"reason": "no tables"}
    )
    assert failed.status == "failed"
    await refused(
        world.runs.write_rendition("knowfeed", grant.id, "index", "0.1", b"x"),
        422,
        "kind_invalid",
    )
    # 表格的内容走事实批；标题只有正文有
    await refused(
        world.runs.write_rendition("knowfeed", grant.id, "table", "0.1", b"x"),
        422,
        "kind_invalid",
    )
    await refused(
        world.runs.write_rendition(
            "knowfeed", grant.id, "pages", "0.1", b"[]", title="年报"
        ),
        422,
        "title_not_allowed",
    )
    assert await world.runs.pending_processing("knowfeed") == []
    kinds = [
        (r.kind, r.status) for r in await world.runs.renditions(ALICE, material.id, 1)
    ]
    assert kinds == [("table", "failed"), ("text", "ready")]
    # 读法内容在库里，不往对象存储写（0043 第五节）
    assert world.objects.data == stored
    reads = await world.runs.reads(ALICE, material.id)
    assert reads[0]["operation"] == "process"

    # 钉住读法：读的就是那一个读法版本
    await world.lib.show(ALICE, material.id, "app:knowbench")
    pinned = item(
        v1,
        "display",
        "app:knowbench",
        rendition_kind="text",
        rendition_no=1,
        rendition_sha256=text.sha256,
    )
    grant = await world.runs.grant_by_user(
        ALICE, "knowbench", "delegation", "t-r", [pinned]
    )
    got = await world.runs.read("knowbench", "r", grant.id, material.id, 1, "rendition")
    assert got.content == b"plain text"
    # 别人的资料没有加工依据
    theirs, _ = await world.upload(BOB)
    await refused(
        world.runs.issue_processing("knowfeed", BOB, theirs.id, 1),
        403,
        "no_processing_consent",
    )


async def test_text_and_pages_live_in_postgres(world):
    """正文一行、按页文字每页一行，都在库里（0043 第五节，数据拓扑第 16.2 节）。"""
    material, v1 = await world.upload(ALICE, "report.csv", process=True)
    grant = await world.runs.issue_processing("knowfeed", ALICE, material.id, 1)
    for body, code in (
        (b"{}", "pages_invalid"),
        (b"[]", "pages_invalid"),
        (b'[{"page": 0, "text": "x"}]', "pages_invalid"),
        (b'[{"page": 1, "text": "x", "more": 1}]', "pages_invalid"),
        (b'[{"page": 1, "text": "a"}, {"page": 1, "text": "b"}]', "pages_duplicate"),
    ):
        await refused(
            world.runs.write_rendition("knowfeed", grant.id, "pages", "p", body),
            422,
            code,
        )
    await refused(
        world.runs.write_rendition("knowfeed", grant.id, "text", "t", b"\xff\xfe"),
        422,
        "text_invalid",
    )
    # 页的先后不影响摘要；NUL 存不进 PostgreSQL，去掉并照实记下
    given = [
        {"page": 3, "text": "第三页：渠道库存"},
        {"page": 1, "text": "封面\x00"},
        {"page": 2, "text": ""},
    ]
    pages = await world.runs.write_rendition(
        "knowfeed", grant.id, "pages", "p-1", json.dumps(given).encode()
    )
    expected = (Page(1, "封面"), Page(2, ""), Page(3, "第三页：渠道库存"))
    assert pages.sha256 == pages_digest(expected)
    assert pages.detail == {"pages": 3, "nul_removed": 1}
    text = await world.runs.write_rendition(
        "knowfeed",
        grant.id,
        "text",
        "t-1",
        "正文".encode(),
        title="2025 年年度报告",
        published_on=date(2026, 3, 28),
    )
    assert text.sha256 == text_digest(
        TextReading("正文", "2025 年年度报告", date(2026, 3, 28))
    )
    again = await world.runs.write_rendition(
        "knowfeed", grant.id, "pages", "p-2", b'[{"page": 1, "text": "new"}]'
    )
    assert again.rendition_no == 2

    await world.lib.show(ALICE, material.id, "app:knowbench")
    run = await world.runs.grant_by_user(
        ALICE,
        "knowbench",
        "delegation",
        "t-p",
        [
            item(
                v1,
                "display",
                "app:knowbench",
                rendition_kind="pages",
                rendition_no=1,
                rendition_sha256=pages.sha256,
            )
        ],
    )
    # 钉了第 1 版读法的读第 1 版，可只取几页
    got = await world.runs.read_reading(
        "knowbench", "r", run.id, material.id, 1, "pages", page_from=2, page_to=3
    )
    assert (got.rendition.rendition_no, got.total_pages) == (1, 3)
    assert got.pages == expected[1:]
    raw = await world.runs.read("knowbench", "r", run.id, material.id, 1, "rendition")
    assert raw.media_type == "application/json"
    assert json.loads(raw.content or b"") == [
        {"page": p.number, "text": p.text} for p in expected
    ]
    # 没钉正文的读最新就绪的那一版，标题、发布日期一起回
    got = await world.runs.read_reading(
        "knowbench", "r", run.id, material.id, 1, "text"
    )
    assert got.text == TextReading("正文", "2025 年年度报告", date(2026, 3, 28))

    hits, searched = await world.runs.search_pages(
        "knowbench", "r", [(run, run.items[0])], ["渠道", "没有的词"]
    )
    assert searched == 1
    assert [(h.rendition.rendition_no, p.number) for h, p in hits] == [(1, 3)]

    # 依法清除：库里的读法内容一起删
    await world.runs.purge_material(
        "admin-1", material.id, "依法删除", personal_info=True
    )
    remaining = await sql(
        world.db,
        "SELECT (SELECT count(*) FROM user_data_page p JOIN user_data_rendition r"
        " ON r.id = p.rendition_id WHERE r.version_id = :v)"
        " + (SELECT count(*) FROM user_data_text t JOIN user_data_rendition r"
        " ON r.id = t.rendition_id WHERE r.version_id = :v)",
        v=uuid.UUID(v1.id),
    )
    assert remaining == 0


async def test_outputs_are_idempotent_immutable_and_convert_into_data(world):
    material, version = await world.runs.write_output(
        "knowbench", ALICE, "task-1", "run-1", "report.md", b"# result\n"
    )
    assert (material.content_origin, material.in_output_zone) == ("output", True)
    again, _ = await world.runs.write_output(
        "knowbench", ALICE, "task-1", "run-1", "report.md", b"# result\n"
    )
    assert again.id == material.id
    await refused(
        world.runs.write_output(
            "knowbench", ALICE, "task-1", "run-1", "report.md", b"# other\n"
        ),
        409,
        "output_exists",
    )
    await refused(
        world.runs.write_output("knowmart", ALICE, "t", "r", "x.md", b"x"),
        403,
        "service_scope_denied",
    )
    shelf = await world.lib.shelf(ALICE, "output")
    assert [e.material.id for e in shelf] == [material.id]
    await refused(
        world.lib.add_version(ALICE, material.id, None, b"# changed\n"),
        422,
        "output_immutable",
    )
    await world.lib.show(ALICE, material.id, "app:knowbench")
    await refused(
        world.runs.grant_by_user(
            ALICE,
            "knowbench",
            "delegation",
            "t",
            [item(version, "display", "app:knowbench")],
        ),
        422,
        "output_not_input",
    )

    # 转为数据：新的一份，进「我上传的」；上游有只许本人用的，不能升档
    data, data_v1 = await world.upload(ALICE, "input.csv")
    await world.lib.show(ALICE, data.id, "app:knowbench")
    grant = await world.runs.grant_by_user(
        ALICE,
        "knowbench",
        "delegation",
        "t-2",
        [item(data_v1, "display", "app:knowbench")],
    )
    derived, _ = await world.runs.write_output(
        "knowbench",
        ALICE,
        "t-2",
        "run-1",
        "table.csv",
        CSV,
        provenance={"expert_id": "fa-1", "input_set_id": grant.input_set_id},
    )
    await refused(
        world.runs.convert(ALICE, derived.id, license_="sale", attest=True),
        422,
        "license_above_upstream",
    )
    converted, _ = await world.runs.convert(ALICE, derived.id)
    assert converted.license == "self" and not converted.in_output_zone
    assert converted.provenance["converted_from"] == derived.id
    entry = await world.lib.get(ALICE, converted.id)
    assert entry.category == "uploaded"
    assert await world.lib.uses(ALICE, converted.id) == []  # 产出转来的不默认展示


async def test_owner_delete_keeps_buyers_but_disable_stops_everyone(world):
    material, v1 = await world.upload(ALICE, license_="sale", attest=True)
    await world.lib.purchase(ALICE, material.id, BOB, "tx-1")
    await world.lib.show(BOB, material.id, "app:knowbench")
    ask = [item(v1, "display", "app:knowbench")]
    grant = await world.runs.grant_by_user(BOB, "knowbench", "delegation", "b-1", ask)
    await world.lib.remove(ALICE, material.id)
    got = await world.runs.read(
        "knowbench", "r", grant.id, material.id, None, "content"
    )
    assert got.content == CSV
    events = await world.runs.deletions(ALICE)
    assert [(e["action"], e["reason"]) for e in events] == [
        ("relation", "owner-removed")
    ]

    await world.runs.disable("admin-1", material.id, "侵权投诉 #12")
    await refused(
        world.runs.read("knowbench", "r", grant.id, material.id, None, "content"),
        403,
        "grant_inactive",
    )
    assert (await world.runs.deletions(ALICE))[-1]["action"] == "disable"


async def test_purge_respects_other_holders_unless_personal_info(world):
    sold, _ = await world.upload(ALICE, "sold.csv", license_="sale", attest=True)
    await world.lib.purchase(ALICE, sold.id, BOB, "tx-1")
    mine, mine_v1 = await world.upload(ALICE, "mine.csv", CSV + b"2,2\n")
    assert mine_v1.object_key in world.objects.data

    purged = await world.runs.purge_subject("admin-1", ALICE, "account-closed")
    assert purged == 1
    assert mine_v1.object_key not in world.objects.data
    # 卖家注销，买家照旧
    assert (await world.lib.get(BOB, sold.id)).category == "purchased"
    events = await world.runs.deletions(ALICE)
    purge = [e for e in events if e["action"] == "purge"]
    assert [(e["material_id"], e["state"], e["pending"]) for e in purge] == [
        (mine.id, "processing", ["ragflow", "knowfeed"])
    ]

    # 内容本身是必须删的个人信息：已卖出的也删，买家的使用权结束
    assert await world.runs.purge_material(
        "admin-1", sold.id, "personal-info", personal_info=True
    )
    await refused(world.lib.get(BOB, sold.id), 404, "item_not_found")
    name = await sql(
        world.db,
        "SELECT name FROM user_data_item WHERE id = :id AND purged_at IS NOT NULL",
        id=uuid.UUID(sold.id),
    )
    assert name == "(cleared)"


async def test_migration_0011_downgrades_and_upgrades_again():
    """0011 起的迁移能退能进：退掉时新表收干净，旧表还在；再进一次照样建起来。"""
    url = os.environ.get("DELIVERY_TEST_DATABASE_URL")
    if not url:
        pytest.skip("set DELIVERY_TEST_DATABASE_URL to a disposable *_tests database")
    parsed = make_url(url)
    assert parsed.database and parsed.database.endswith("_tests")
    schema = "runs_test_" + uuid.uuid4().hex
    engine = create_async_engine(
        parsed.set(drivername="postgresql+asyncpg"),
        connect_args={"server_settings": {"search_path": schema + ",public"}},
    )
    paths = sorted((ROOT / "alembic/versions").glob("20*.py"))
    target = [p for p in paths if p.name.startswith("20261027_0011")]
    assert len(target) == 1
    later = [p for p in paths if p.name > target[0].name]

    def load(path):
        spec = importlib.util.spec_from_file_location(path.stem, path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def run(connection, step):
        with Operations.context(MigrationContext.configure(connection)):
            step()

    def upgrade_all():
        for path in paths:
            load(path).upgrade()

    try:
        async with engine.begin() as c:
            await c.execute(text(f'CREATE SCHEMA "{schema}"'))
            await c.execute(
                text('CREATE EXTENSION IF NOT EXISTS "uuid-ossp" WITH SCHEMA public')
            )
            await c.run_sync(run, upgrade_all)
            for path in reversed(target + later):
                await c.run_sync(run, load(path).downgrade)
            tables = await c.execute(
                text(
                    "SELECT table_name FROM information_schema.tables"
                    " WHERE table_schema = :s"
                ),
                {"s": schema},
            )
            names = {r[0] for r in tables}
            assert "user_data_run_grant" not in names
            assert "user_data_resolution" not in names
            assert "user_data_item" in names
            for path in target + later:
                await c.run_sync(run, load(path).upgrade)
        db = make_session_factory(engine)
        assert await sql(db, "SELECT count(*) FROM user_data_run_grant") == 0
        assert await sql(db, "SELECT count(*) FROM user_data_confirmation") == 0
    finally:
        async with engine.begin() as c:
            await c.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
        await engine.dispose()


# —— 交付审查 10-30：冻结只一次、冻结许可、重跑授权、按版本挑使用权 ——


async def test_freezing_again_returns_what_was_frozen_and_preview_writes_nothing(world):
    """第一次冻结后作者改许可、传新版再交：回的仍是第一次冻结的版本、许可与那一版的摘要
    （原来按现在的版本取摘要，与冻结的版本对不上，买家请专家报 sha256_mismatch）。"""
    material, v1 = await world.upload(ALICE, "binding.csv")  # 只许本人用
    await world.lib.upsert_expert("fa-1", ALICE, "财务分析与预测", None)
    await world.lib.show(ALICE, material.id, "expert:fa-1")
    [(preview, sha)] = await world.lib.freeze_binding(ALICE, "fa-1", "1", preview=True)
    assert (preview.version_no, preview.scope["license"], sha) == (1, "self", v1.sha256)
    assert await world.lib.repo.named(ALICE, "expert:fa-1@1") == []  # 预查不写
    [(first, sha1)] = await world.lib.freeze_binding(ALICE, "fa-1", "1")
    await world.lib.set_license(ALICE, material.id, "via_expert", attest=True)
    await world.lib.add_version(ALICE, material.id, None, CSV + b"7,7\n")
    [(again, sha2)] = await world.lib.freeze_binding(ALICE, "fa-1", "1")
    assert (again.version_no, again.scope["license"], sha2) == (1, "self", v1.sha256)
    assert again.id == first.id and sha1 == sha2
    # 摘要与冻结的版本对得上：作者自己照清单请得到
    await world.runs.grant_by_user(
        ALICE,
        "knowbench",
        "delegation",
        "task-1",
        [
            GrantRequestItem(
                pin=InputPin(material.id, again.version_no, sha2),
                basis="frozen_binding",
                basis_ref="expert:fa-1@1",
            )
        ],
    )
    # 冻结时只许本人用：现在的许可改高了，冻结的那一档照旧不够给别人用
    [
        (owner, mid, vno, state, license_, shareable, frozen)
    ] = await world.lib.binding_states(
        "knowmart", [(ALICE, material.id, 1, "expert:fa-1@1")]
    )
    assert (state, license_, frozen, shareable) == ("live", "via_expert", "self", False)
    [(*_, shareable_now, no_frozen)] = await world.lib.binding_states(
        "knowlab", [(ALICE, material.id, 1, None)]
    )
    assert (shareable_now, no_frozen) == (True, None)
    # 这一版没冻结这一份：当没绑
    [(*_, state2, _, _, _)] = await world.lib.binding_states(
        "knowmart", [(ALICE, material.id, 1, "expert:fa-1@9")]
    )
    assert state2 == "gone"


async def test_a_rerun_case_with_no_consented_item_still_gets_a_machine_grant(world):
    """平台重跑的一条用例全是冻结绑定（或公开原件、引用），没有凭平台同意的项：照
    knowlab 交的 platform_use 记过程，签得下；撤过程时一并撤。"""
    material, v1 = await world.upload(
        ALICE, "binding.csv", license_="via_expert", attest=True
    )
    await world.lib.upsert_expert("fa-1", ALICE, "财务分析与预测", None)
    await world.lib.show(ALICE, material.id, "expert:fa-1")
    await world.lib.freeze_binding(ALICE, "fa-1", "1")
    rerun = "platform:knowlab:rerun:d-9"
    frozen = [item(v1, "frozen_binding", "expert:fa-1@1")]
    await refused(
        world.runs.grant_by_service(
            "knowlab", "platform", ALICE, "d-9/c1", frozen, reader_role="machine"
        ),
        422,
        "basis_ref_invalid",
    )
    # 没有凭平台同意的项：这次过程本人的同意也要在（没留过、撤了都不签）
    await refused(
        world.runs.grant_by_service(
            "knowlab",
            "platform",
            ALICE,
            "d-9/c1",
            frozen,
            reader_role="machine",
            platform=rerun,
        ),
        403,
        "outside_consent",
    )
    [marker] = await world.lib.consent_platform(ALICE, "knowlab", "rerun", "d-9", [])
    assert (marker.use, marker.material_id) == (rerun, None)
    machine = await world.runs.grant_by_service(
        "knowlab",
        "platform",
        ALICE,
        "d-9/c1",
        frozen,
        reader_role="machine",
        platform=rerun,
    )
    assert (machine.platform_use, machine.items[0].basis) == (rerun, "frozen_binding")
    await refused(  # 运营照旧只认平台同意覆盖的
        world.runs.grant_by_service(
            "knowlab",
            "platform",
            ALICE,
            "d-9/op",
            frozen,
            reader_role="operator",
            platform=rerun,
        ),
        422,
        "basis_not_allowed",
    )
    await world.runs.end_platform("knowlab", ALICE, "rerun", "d-9", "finished")
    await refused(
        world.runs.read("knowbench", "r", machine.id, material.id, 1, "content"),
        403,
        "grant_inactive",
    )


async def test_platform_consent_picks_the_holding_that_covers_the_version(world):
    """买过同一份的两个版本：交审时按版本挑覆盖的那条使用权（原来取第一条，可能不覆盖
    要的那一版，502）。"""
    material, v1 = await world.upload(
        ALICE, "for-sale.csv", license_="sale", attest=True
    )
    await world.lib.show(ALICE, material.id, "market")
    await world.lib.purchase(ALICE, material.id, BOB, "T-1")
    await world.lib.add_version(ALICE, material.id, None, CSV + b"8,8\n")
    await world.lib.purchase(ALICE, material.id, BOB, "T-2")
    holdings = await world.lib.repo.holdings_of(BOB, material.id)
    assert sorted(h.version_no for h in holdings) == [1, 2]
    for version in (1, 2):
        [made] = await world.lib.consent_platform(
            BOB, "knowlab", "review", f"d-{version}", [(material.id, version)]
        )
        assert made.version_no == version


async def test_the_frozen_license_only_goes_up_with_an_attestation(world):
    """自用转公开时作者把冻结的许可往高改（决定 10-30）：只许往高、要勾「我有权」、
    现在的许可够；留许可留痕。"""
    material, v1 = await world.upload(ALICE, "binding.csv")  # 只许本人用
    await world.lib.upsert_expert("fa-1", ALICE, "财务分析与预测", None)
    await world.lib.show(ALICE, material.id, "expert:fa-1")
    await world.lib.freeze_binding(ALICE, "fa-1", "1")
    raise_ = world.lib.raise_frozen_license
    await refused(
        raise_("knowlab", ALICE, "fa-1", "1", material.id, "via_expert", attest=False),
        422,
        "attest_required",
    )
    await refused(  # 现在的许可还只许本人用
        raise_("knowlab", ALICE, "fa-1", "1", material.id, "via_expert", attest=True),
        422,
        "license_too_low",
    )
    await refused(
        raise_("knowmart", ALICE, "fa-1", "1", material.id, "via_expert", attest=True),
        403,
        "service_scope_denied",
    )
    await world.lib.set_license(ALICE, material.id, "via_expert", attest=True)
    raised = await raise_(
        "knowlab", ALICE, "fa-1", "1", material.id, "via_expert", attest=True
    )
    assert (raised.version_no, raised.scope["license"]) == (1, "via_expert")
    await refused(
        raise_("knowlab", ALICE, "fa-1", "1", material.id, "self", attest=True),
        422,
        "license_not_higher",
    )
    [(*_, shareable, frozen)] = await world.lib.binding_states(
        "knowmart", [(ALICE, material.id, 1, "expert:fa-1@1")]
    )
    assert (shareable, frozen) == (True, "via_expert")
    history = await world.lib.history(ALICE, material.id)
    assert any(
        h["after"].get("frozen") == "expert:fa-1@1" and h["after"].get("attested")
        for h in history
    )
    # 别人照冻结的这一份请得到
    await world.runs.grant_by_user(
        BOB,
        "knowbench",
        "delegation",
        "t-1",
        [item(v1, "frozen_binding", "expert:fa-1@1")],
    )


async def test_an_undelivered_freeze_is_voided_but_not_one_others_used(world):
    """没交到 knowmart 的那次冻结作者撤回时作废，下次照那时的绑定重冻；有别人请过的
    不许作废（决定 10-30）。"""
    material, v1 = await world.upload(
        ALICE, "binding.csv", license_="via_expert", attest=True
    )
    await world.lib.upsert_expert("fa-1", ALICE, "财务分析与预测", None)
    await world.lib.show(ALICE, material.id, "expert:fa-1")
    await world.lib.freeze_binding(ALICE, "fa-1", "1")
    await world.lib.freeze_binding(ALICE, "fa-1", "2")
    await world.runs.grant_by_user(
        BOB,
        "knowbench",
        "delegation",
        "t-1",
        [item(v1, "frozen_binding", "expert:fa-1@2")],
    )
    await refused(
        world.runs.void_frozen("knowlab", ALICE, "fa-1", "2"), 409, "frozen_in_use"
    )
    assert await world.runs.void_frozen("knowlab", ALICE, "fa-1", "1") == 1
    assert await world.lib.repo.named(ALICE, "expert:fa-1@1") == []
    # 作废后传了新版：再冻冻的是现在那一版
    await world.lib.add_version(ALICE, material.id, None, CSV + b"7,7\n")
    [(again, _)] = await world.lib.freeze_binding(ALICE, "fa-1", "1")
    assert again.version_no == 2


async def test_purge_keeps_content_frozen_to_an_expert_nobody_has_asked_yet(world):
    # 账 185：冻结给专家的这一份，就算还没人请过（没签过授权），非个人信息的清除也只
    # 删掉、不真删内容；依法清除个人信息照旧真删
    material, v1 = await world.upload(
        ALICE, "binding.csv", license_="via_expert", attest=True
    )
    await world.lib.upsert_expert("fa-1", ALICE, "财务分析与预测", None)
    await world.lib.show(ALICE, material.id, "expert:fa-1")
    await world.lib.freeze_binding(ALICE, "fa-1", "1")

    assert not await world.runs.purge_material(
        "admin-1", material.id, "清理", personal_info=False
    )
    assert v1.object_key in world.objects.data
    assert await world.runs.purge_material(
        "admin-1", material.id, "personal-info", personal_info=True
    )
    assert v1.object_key not in world.objects.data


async def test_purge_ignores_an_unfrozen_binding(world):
    # 只「绑给专家」、还没交付冻结的，下一版才会冻结：照常清除
    material, v1 = await world.upload(ALICE, "draft.csv")
    await world.lib.upsert_expert("fa-1", ALICE, "财务分析与预测", None)
    await world.lib.show(ALICE, material.id, "expert:fa-1")
    assert await world.runs.purge_material(
        "admin-1", material.id, "清理", personal_info=False
    )
    assert v1.object_key not in world.objects.data
