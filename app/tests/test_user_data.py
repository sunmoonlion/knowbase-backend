"""用户资料（SDD 0042）。

上传与本人读删、同意依据、运行授权、凭授权读、买来复制、本人令牌、接口。

不需要数据库与对象存储：记录与对象都用内存替身；数据库那一份见 test_user_data_db.py。
"""

from __future__ import annotations

import hashlib
import io
import json
import zipfile
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from fastapi import FastAPI

from app.application.services.user_data_service import UserDataService
from app.domain.security import Principal
from app.domain.user_data import (
    MAX_ITEM_BYTES,
    Consent,
    Grant,
    Item,
    Owner,
    Pin,
    ReadEvent,
    UserDataError,
    conflict,
    not_found,
)
from app.infrastructure.security.user_token import UserTokenVerifier
from app.infrastructure.storage.user_objects import S3UserObjects, sign
from app.interfaces.endpoints import user_data_routes
from app.interfaces.errors.exception_handlers import register_exception_handlers
from core.config import Settings

ALICE = Owner("alice", "knowbench")
ALICE_LAB = Owner("alice", "knowlab")
BOB = Owner("bob", "knowbench")
CSV = b"year,revenue\n2023,10\n"


def xlsx(*sheets: str) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as book:
        names = "".join(
            f'<sheet name="{n}" sheetId="{i}"/>' for i, n in enumerate(sheets)
        )
        book.writestr(
            "xl/workbook.xml", f"<workbook><sheets>{names}</sheets></workbook>"
        )
    return buffer.getvalue()


class MemoryRepo:
    """照端口的约定：删除在用的抛 409，签授权时引到已删的抛 404。"""

    def __init__(self) -> None:
        self.item_rows: dict[str, Item] = {}
        self.consents: dict[str, Consent] = {}
        self.grants: dict[str, Grant] = {}
        self.reads: list[ReadEvent] = []

    async def add_item(self, item: Item) -> Item:
        self.item_rows[item.id] = item
        return item

    async def get_item(self, item_id: str) -> Item | None:
        return self.item_rows.get(item_id)

    async def items(self, ids: list[str]) -> dict[str, Item]:
        return {i: self.item_rows[i] for i in ids if i in self.item_rows}

    async def list_items(self, owner: Owner) -> list[Item]:
        mine = [
            i for i in self.item_rows.values() if i.owner == owner and not i.deleted_at
        ]
        return sorted(mine, key=lambda i: i.created_at, reverse=True)

    async def bytes_used(self, owner: Owner) -> int:
        return sum(i.size_bytes for i in await self.list_items(owner))

    async def find_by_origin(self, owner: Owner, origin_ref: str) -> Item | None:
        return next(
            (
                i
                for i in self.item_rows.values()
                if i.owner == owner and i.origin_ref == origin_ref
            ),
            None,
        )

    async def delete_item(self, item_id: str, now: datetime) -> None:
        item = self.item_rows.get(item_id)
        if item is None or item.deleted_at is not None:
            raise not_found()
        granted = any(g.active(now) and g.pin(item_id) for g in self.grants.values())
        consented = any(
            c.withdrawn_at is None and any(p.item_id == item_id for p in c.pins)
            for c in self.consents.values()
        )
        if granted or consented:
            raise conflict("in_use", "in use")
        self.item_rows[item_id] = Item(**{**item.__dict__, "deleted_at": now})

    async def add_consent(self, consent: Consent) -> Consent:
        self._live(consent.pins)
        self.consents[consent.id] = consent
        return consent

    async def get_consent(self, consent_id: str) -> Consent | None:
        return self.consents.get(consent_id)

    async def withdraw_consent(self, consent_id: str, now: datetime) -> None:
        c = self.consents[consent_id]
        self.consents[consent_id] = Consent(**{**c.__dict__, "withdrawn_at": now})

    async def add_grant(self, grant: Grant) -> Grant:
        self._live(grant.pins)
        self.grants[grant.id] = grant
        return grant

    async def get_grant(self, grant_id: str) -> Grant | None:
        return self.grants.get(grant_id)

    async def revoke_grant(self, grant_id: str, now: datetime) -> None:
        g = self.grants[grant_id]
        self.grants[grant_id] = Grant(**{**g.__dict__, "revoked_at": now})

    async def record_read(self, event: ReadEvent) -> None:
        self.reads.append(event)

    def _live(self, pins: tuple[Pin, ...]) -> None:
        for p in pins:
            item = self.item_rows.get(p.item_id)
            if item is None or item.deleted_at is not None:
                raise not_found()


class MemoryObjects:
    def __init__(self) -> None:
        self.data: dict[str, bytes] = {}

    async def put(self, key: str, content: bytes, media_type: str) -> None:
        self.data[key] = content

    async def get(self, key: str) -> bytes:
        return self.data[key]

    async def delete(self, key: str) -> None:
        self.data.pop(key, None)

    async def copy(self, source_key: str, target_key: str) -> None:
        self.data[target_key] = self.data[source_key]


class Clock:
    def __init__(self) -> None:
        self.now = datetime(2026, 10, 23, 12, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.now


@pytest.fixture
def world():
    repo, objects, clock = MemoryRepo(), MemoryObjects(), Clock()
    counter = iter(range(1, 10_000))
    service = UserDataService(
        repo,
        objects,
        prefix="users",
        clock=clock,
        new_id=lambda: f"00000000-0000-4000-8000-{next(counter):012d}",
    )
    return service, repo, objects, clock


def pin(item: Item) -> Pin:
    return Pin(item.id, item.sha256)


async def refused(work, status: int, code: str) -> None:
    with pytest.raises(UserDataError) as caught:
        await work
    assert (caught.value.status, caught.value.code) == (status, code)


# —— 本人 ——


async def test_upload_lists_and_reads_only_in_its_own_partition(world):
    service, repo, objects, _ = world
    item = await service.upload(ALICE, "营收.csv", CSV)
    assert item.sha256 == hashlib.sha256(CSV).hexdigest()
    assert item.media_type.startswith("text/csv") and item.origin == "upload"
    # 对象键按用户摘要、应用分开，键里没有用户编号原值
    assert "alice" not in item.object_key and "/knowbench/" in item.object_key
    assert [i.id for i in await service.items_of(ALICE)] == [item.id]
    # 别人、同一个人的别的应用分区都看不到（与没有一样）
    for other in (BOB, ALICE_LAB):
        assert await service.items_of(other) == []
        await refused(service.get(other, item.id), 404, "item_not_found")
    got, content = await service.download(ALICE, item.id)
    assert content == CSV
    assert (
        repo.reads[-1].operation == "owner-download"
        and repo.reads[-1].reader == "alice"
    )


@pytest.mark.parametrize(
    ("name", "content", "code"),
    [
        ("a.exe", b"MZ", "type_not_allowed"),
        ("a.pdf", b"not a pdf", "content_mismatch"),
        ("a.csv", "营收".encode("gbk"), "not_utf8"),
        ("../a.csv", CSV, "name_invalid"),
        ("a.csv", b"", "empty"),
    ],
)
async def test_bad_uploads_are_refused(world, name, content, code):
    service, *_ = world
    with pytest.raises(UserDataError) as caught:
        await service.upload(ALICE, name, content)
    assert caught.value.code == code


async def test_quota_counts_live_items_of_the_partition(world, monkeypatch):
    service, *_ = world
    monkeypatch.setattr("app.domain.user_data.QUOTA_BYTES", len(CSV) * 2)
    first = await service.upload(ALICE, "a.csv", CSV)
    await service.upload(ALICE, "b.csv", CSV)
    await refused(service.upload(ALICE, "c.csv", CSV), 413, "quota_exceeded")
    await service.upload(ALICE_LAB, "c.csv", CSV)  # 别的分区另算
    await service.delete(ALICE, first.id)
    await service.upload(ALICE, "c.csv", CSV)  # 删了就腾出来


async def test_stored_content_that_does_not_match_is_not_served(world):
    service, _, objects, _ = world
    item = await service.upload(ALICE, "a.csv", CSV)
    objects.data[item.object_key] = b"tampered"
    await refused(service.download(ALICE, item.id), 503, "storage_mismatch")


async def test_delete_waits_for_grants_and_consents(world):
    service, _, objects, clock = world
    item = await service.upload(ALICE, "a.csv", CSV)
    grant = await service.grant_by_user(
        ALICE, "delegation", "knowbench", "task-1", (pin(item),)
    )
    await refused(service.delete(ALICE, item.id), 409, "in_use")
    await service.revoke_by_user(ALICE, grant.id)
    lab = await service.upload(ALICE_LAB, "b.csv", CSV)
    consent = await service.consent(ALICE_LAB, "knowlab-test", "idea-1@v2", (pin(lab),))
    await refused(service.delete(ALICE_LAB, lab.id), 409, "in_use")
    await service.withdraw_consent(ALICE_LAB, consent.id)
    await service.delete(ALICE_LAB, lab.id)
    await service.delete(ALICE, item.id)
    assert item.object_key not in objects.data
    # 过期的授权不挡删除
    again = await service.upload(ALICE, "c.csv", CSV)
    await service.grant_by_user(ALICE, "session", "knowbench", "s-1", (pin(again),), 60)
    clock.now += timedelta(seconds=61)
    await service.delete(ALICE, again.id)


# —— 运行授权 ——


async def test_user_grants_read_the_right_partition_and_only_own_pins(world):
    service, *_ = world
    item = await service.upload(ALICE, "a.csv", CSV)
    lab = await service.upload(ALICE_LAB, "b.csv", CSV)
    bobs = await service.upload(BOB, "c.csv", CSV)
    # 委托、会话读 knowbench 云端知识库；孵化读 knowlab 知识库；测试不由用户签
    await refused(
        service.grant_by_user(ALICE, "incubation", "knowbench", "r", (pin(item),)),
        422,
        "partition_invalid",
    )
    await refused(
        service.grant_by_user(ALICE, "test", "knowbench", "r", (pin(item),)),  # type: ignore[arg-type]
        422,
        "kind_invalid",
    )
    incubation = await service.grant_by_user(
        ALICE_LAB, "incubation", "knowbench", "r", (pin(lab),)
    )
    assert incubation.scope == "format"
    await refused(
        service.grant_by_user(ALICE, "delegation", "knowbench", "r", (pin(bobs),)),
        404,
        "item_not_found",
    )
    await refused(
        service.grant_by_user(
            ALICE, "delegation", "knowbench", "r", (Pin(item.id, "0" * 64),)
        ),
        409,
        "sha256_mismatch",
    )
    await refused(
        service.grant_by_user(
            ALICE, "delegation", "knowbench", "r", (pin(item), pin(item))
        ),
        422,
        "pins_duplicate",
    )


async def test_grant_lifetime_is_capped_by_kind(world):
    service, _, _, clock = world
    item = await service.upload(ALICE, "a.csv", CSV)
    grant = await service.grant_by_user(
        ALICE, "session", "knowbench", "s", (pin(item),), 7 * 24 * 3600
    )
    assert grant.expires_at == clock.now + timedelta(hours=12)


async def test_test_grants_stay_inside_the_users_consent(world):
    service, *_ = world
    a = await service.upload(ALICE_LAB, "a.csv", CSV)
    b = await service.upload(ALICE_LAB, "b.csv", b"x,y\n")
    consent = await service.consent(ALICE_LAB, "knowlab-test", "idea-1@v2", (pin(a),))
    grant = await service.grant_test(
        "svc-knowlab", "alice", consent.id, "knowbench", "test-5", (pin(a),)
    )
    assert (grant.kind, grant.owner, grant.consent_id) == (
        "test",
        ALICE_LAB,
        consent.id,
    )
    await refused(
        service.grant_test(
            "svc-knowlab", "alice", consent.id, "knowbench", "test-6", (pin(b),)
        ),
        403,
        "outside_consent",
    )
    # 同意依据不是这个人的、用途不对、撤了：都当没有
    await refused(
        service.grant_test(
            "svc-knowlab", "bob", consent.id, "knowbench", "t", (pin(a),)
        ),
        404,
        "consent_not_found",
    )
    other = await service.consent(ALICE_LAB, "something-else", "x", (pin(b),))
    await refused(
        service.grant_test(
            "svc-knowlab", "alice", other.id, "knowbench", "t", (pin(b),)
        ),
        404,
        "consent_not_found",
    )
    await service.withdraw_consent(ALICE_LAB, consent.id)
    await refused(
        service.grant_test(
            "svc-knowlab", "alice", consent.id, "knowbench", "t", (pin(a),)
        ),
        404,
        "consent_not_found",
    )
    # 测试授权只有签它的服务能撤
    await refused(
        service.revoke_by_service("svc-other", grant.id), 404, "grant_not_found"
    )
    await service.revoke_by_service("svc-knowlab", grant.id)


async def test_reading_needs_an_active_grant_for_that_reader_and_item(world):
    service, repo, _, clock = world
    item = await service.upload(ALICE, "a.csv", CSV)
    other = await service.upload(ALICE, "b.csv", b"q\n")
    grant = await service.grant_by_user(
        ALICE, "delegation", "knowbench", "task-1", (pin(item),)
    )
    got, content = await service.read(
        "knowbench", "svc-bench", grant.id, item.id, "content"
    )
    assert content == CSV
    assert (repo.reads[-1].operation, repo.reads[-1].grant_id) == ("content", grant.id)
    await refused(
        service.read("knowlab", "svc", grant.id, item.id, "content"),
        404,
        "grant_not_found",
    )
    await refused(
        service.read("knowbench", "svc", grant.id, other.id, "content"),
        404,
        "item_not_found",
    )
    await service.revoke_by_user(ALICE, grant.id)
    await refused(
        service.read("knowbench", "svc", grant.id, item.id, "content"),
        403,
        "grant_inactive",
    )
    fresh = await service.grant_by_user(
        ALICE, "session", "knowbench", "s", (pin(item),), 60
    )
    clock.now += timedelta(minutes=2)
    await refused(
        service.read("knowbench", "svc", fresh.id, item.id, "content"),
        403,
        "grant_inactive",
    )


async def test_incubation_grants_see_formats_only(world):
    service, repo, _, _ = world
    book = await service.upload(ALICE_LAB, "三表.xlsx", xlsx("资产负债表", "利润表"))
    text = await service.upload(ALICE_LAB, "营收.csv", CSV)
    grant = await service.grant_by_user(
        ALICE_LAB, "incubation", "knowbench", "hatch-1", (pin(book), pin(text))
    )
    await refused(
        service.read("knowbench", "svc", grant.id, book.id, "content"),
        403,
        "format_only",
    )
    _, view = await service.read("knowbench", "svc", grant.id, book.id, "format")
    assert view.sheets == ("资产负债表", "利润表") and view.header is None
    _, view = await service.read("knowbench", "svc", grant.id, text.id, "format")
    assert view.header == "year,revenue"
    assert repo.reads[-1].operation == "format"


# —— 买来复制 ——


async def test_a_purchase_copies_once_into_the_buyers_chosen_knowledge_base(world):
    service, repo, objects, _ = world
    seller = Owner("seller", "knowmart")
    goods = await service.upload(seller, "年报整理.csv", CSV)
    copied = await service.purchase(
        "svc-mart", "seller", goods.id, "alice", "knowlab", "tx-1"
    )
    assert (copied.owner, copied.origin, copied.origin_ref) == (
        ALICE_LAB,
        "purchase",
        "tx-1",
    )
    assert objects.data[copied.object_key] == CSV and copied.sha256 == goods.sha256
    assert repo.reads[-1].operation == "purchase-copy"
    again = await service.purchase(
        "svc-mart", "seller", goods.id, "alice", "knowlab", "tx-1"
    )
    assert again.id == copied.id  # 同一笔重发不再复制
    # 卖家删了、下架了，买家那份照旧在
    await service.delete(seller, goods.id)
    _, content = await service.download(ALICE_LAB, copied.id)
    assert content == CSV


async def test_purchases_come_from_knowmart_and_go_to_bench_or_lab(world):
    service, *_ = world
    not_for_sale = await service.upload(Owner("seller", "knowbench"), "a.csv", CSV)
    await refused(
        service.purchase("svc", "seller", not_for_sale.id, "alice", "knowbench", "tx"),
        404,
        "item_not_found",
    )
    goods = await service.upload(Owner("seller", "knowmart"), "a.csv", CSV)
    await refused(
        service.purchase("svc", "seller", goods.id, "alice", "knowmart", "tx"),  # type: ignore[arg-type]
        422,
        "buyer_app_invalid",
    )


# —— 本人令牌 ——


class FakeOidc:
    def __init__(self, claims: dict) -> None:
        self.claims = claims
        self.audience: str | None = None

    async def verify_access_token(self, encoded: str, *, audience: str) -> dict:
        self.audience = audience
        return self.claims


def verifier(claims: dict, **settings) -> tuple[UserTokenVerifier, FakeOidc]:
    base = {
        "user_token_audience": "knowbase-client",
        "user_token_apps_json": json.dumps(
            {"bench-client": "knowbench", "lab-client": "knowlab"}
        ),
    }
    oidc = FakeOidc(claims)
    return UserTokenVerifier(Settings(**{**base, **settings}), oidc_client=oidc), oidc  # type: ignore[arg-type]


async def test_a_user_token_names_the_user_and_the_acting_app():
    v, oidc = verifier(
        {"sub": "alice", "azp": "lab-client", "tokenType": "access-token"}
    )
    assert await v.verify("token") == ALICE_LAB
    assert oidc.audience == "knowbase-client"


@pytest.mark.parametrize(
    ("claims", "settings", "status", "code"),
    [
        ({"sub": "alice", "azp": "mart-client"}, {}, 403, "user_token_app_denied"),
        (
            {"sub": "alice", "azp": "bench-client", "tokenType": "id-token"},
            {},
            401,
            "token_invalid",
        ),
        ({"sub": " ", "azp": "bench-client"}, {}, 401, "token_invalid"),
        (
            {"sub": "alice", "azp": "bench-client"},
            {"user_token_audience": ""},
            503,
            "user_token_unconfigured",
        ),
    ],
)
async def test_user_tokens_that_are_refused(claims, settings, status, code):
    v, _ = verifier(claims, **settings)
    from app.application.errors.exceptions import AppException

    with pytest.raises(AppException) as caught:
        await v.verify("token")
    assert (caught.value.status_code, caught.value.code) == (status, code)


# —— 对象存储 ——


def test_sigv4_matches_the_aws_get_object_example():
    """AWS 文档「GET Object」示例（examplebucket、Range 头、2013-05-24）。"""
    headers = sign(
        method="GET",
        host="examplebucket.s3.amazonaws.com",
        path="/test.txt",
        body=b"",
        extra={"Range": "bytes=0-9"},
        region="us-east-1",
        access_key="AKIAIOSFODNN7EXAMPLE",
        secret_key="wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY",
        now=datetime(2013, 5, 24, tzinfo=UTC),
    )
    assert headers["Authorization"].endswith(
        "SignedHeaders=host;range;x-amz-content-sha256;x-amz-date, "
        "Signature=f0e8bdb87c964420e857bd35b5d6ed310bd44f0170aba48dd91039c6036bdb41"
    )


async def test_object_store_puts_gets_copies_and_deletes_in_its_bucket():
    seen: list[httpx.Request] = []

    def answer(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, content=b"body" if request.method == "GET" else b"")

    store = S3UserObjects(
        Settings(
            **{
                "S3_ENDPOINT": "http://minio:9000",
                "S3_ACCESS_KEY_ID": "k",
                "S3_SECRET_ACCESS_KEY": "s",
                "user_data_bucket": "user-data",
            }
        ),
        transport=httpx.MockTransport(answer),
    )
    await store.put("users/ab/x/knowbench/1", b"abc", "text/csv")
    assert await store.get("users/ab/x/knowbench/1") == b"body"
    await store.copy("users/ab/x/knowmart/2", "users/cd/y/knowlab/3")
    await store.delete("users/ab/x/knowbench/1")
    assert [(r.method, r.url.path) for r in seen] == [
        ("PUT", "/user-data/users/ab/x/knowbench/1"),
        ("GET", "/user-data/users/ab/x/knowbench/1"),
        ("PUT", "/user-data/users/cd/y/knowlab/3"),
        ("DELETE", "/user-data/users/ab/x/knowbench/1"),
    ]
    assert seen[0].headers["x-amz-content-sha256"] == hashlib.sha256(b"abc").hexdigest()
    assert seen[2].headers["x-amz-copy-source"] == "/user-data/users/ab/x/knowmart/2"
    assert "x-amz-copy-source" in seen[2].headers["authorization"]


def test_object_store_refuses_to_start_without_configuration():
    with pytest.raises(UserDataError) as caught:
        S3UserObjects(Settings(**{"S3_ENDPOINT": "http://minio:9000"}))
    assert caught.value.code == "storage_unconfigured"


# —— 接口 ——


def service_principal(*scopes: str) -> Principal:
    now = datetime.now(UTC)
    return Principal(
        actor_type="service",
        subject="svc-test",
        issuer="https://casdoor.test",
        app="knowbase",
        surface="internal",
        audience="knowbase",
        scopes=frozenset(scopes),
        authenticated_at=now,
        expires_at=now + timedelta(minutes=5),
        policy_version="service-v1",
    )


@pytest.fixture
def api(world):
    service, repo, objects, clock = world
    app = FastAPI()
    register_exception_handlers(app)
    app.include_router(user_data_routes.router, prefix="/api")
    state = {"owner": ALICE, "principal": service_principal("user-data:read:knowbench")}
    overrides = app.dependency_overrides
    overrides[user_data_routes.internal_host_only] = lambda: None
    overrides[user_data_routes.require_user_owner] = lambda: state["owner"]
    overrides[user_data_routes.get_user_data_service] = lambda: service
    for dep in (
        user_data_routes.require_any_service,
        user_data_routes.require_grant_test_service,
        user_data_routes.require_purchase_service,
    ):
        overrides[dep] = lambda: state["principal"]
    transport = httpx.ASGITransport(app=app)
    client = httpx.AsyncClient(transport=transport, base_url="http://knowbase")
    return client, state, repo


BASE = "/api/internal/v1/user-data"


async def test_api_upload_list_download_and_errors_with_codes(api):
    client, state, _ = api
    created = await client.post(
        f"{BASE}/items", params={"name": "营收.csv"}, content=CSV
    )
    assert created.status_code == 201, created.text
    item = created.json()
    assert (item["app"], item["origin"], item["size_bytes"]) == (
        "knowbench",
        "upload",
        len(CSV),
    )
    listed = (await client.get(f"{BASE}/items")).json()
    assert [i["item_id"] for i in listed] == [item["item_id"]]
    content = await client.get(f"{BASE}/items/{item['item_id']}/content")
    assert (
        content.content == CSV and content.headers["x-content-sha256"] == item["sha256"]
    )
    bad = await client.post(f"{BASE}/items", params={"name": "a.exe"}, content=b"MZ")
    assert (bad.status_code, bad.json()["code"]) == (422, "type_not_allowed")
    state["owner"] = BOB
    missing = await client.get(f"{BASE}/items/{item['item_id']}")
    assert (missing.status_code, missing.json()["code"]) == (404, "item_not_found")


async def test_api_refuses_oversized_bodies_before_reading_them_all(api, monkeypatch):
    client, *_ = api
    monkeypatch.setattr(user_data_routes, "MAX_ITEM_BYTES", 8)
    too_big = await client.post(
        f"{BASE}/items", params={"name": "a.csv"}, content=b"x" * 9
    )
    assert (too_big.status_code, too_big.json()["code"]) == (413, "too_large")
    assert MAX_ITEM_BYTES == 50 * 1024 * 1024


async def test_api_grant_then_read_as_the_reader_app(api):
    client, state, repo = api
    item = (
        await client.post(f"{BASE}/items", params={"name": "a.csv"}, content=CSV)
    ).json()
    pins = [{"item_id": item["item_id"], "sha256": item["sha256"]}]
    grant = await client.post(
        f"{BASE}/grants",
        json={
            "kind": "delegation",
            "reader_app": "knowbench",
            "ref": "task-9",
            "items": pins,
        },
    )
    assert grant.status_code == 201, grant.text
    gid = grant.json()["grant_id"]
    view = (await client.get(f"{BASE}/grants/{gid}")).json()
    assert [g["name"] for g in view["granted"]] == ["a.csv"]
    read = await client.get(f"{BASE}/grants/{gid}/items/{item['item_id']}/content")
    assert read.content == CSV and repo.reads[-1].reader == "svc-test"
    # 服务身份要有且只有一个 user-data:read:<应用>
    state["principal"] = service_principal(
        "user-data:read:knowbench", "user-data:read:knowlab"
    )
    denied = await client.get(f"{BASE}/grants/{gid}/items/{item['item_id']}/content")
    assert (denied.status_code, denied.json()["code"]) == (403, "service_scope_denied")
    state["principal"] = service_principal("user-data:read:knowlab")
    other = await client.get(f"{BASE}/grants/{gid}/items/{item['item_id']}/content")
    assert (other.status_code, other.json()["code"]) == (404, "grant_not_found")


async def test_api_test_grant_and_purchase_as_services(api):
    client, state, _ = api
    state["owner"] = ALICE_LAB
    item = (
        await client.post(f"{BASE}/items", params={"name": "a.csv"}, content=CSV)
    ).json()
    pins = [{"item_id": item["item_id"], "sha256": item["sha256"]}]
    consent = (
        await client.post(
            f"{BASE}/consents",
            json={"purpose": "knowlab-test", "ref": "idea-1@v1", "items": pins},
        )
    ).json()
    state["principal"] = service_principal("user-data:grant-test")
    grant = await client.post(
        f"{BASE}/test-grants",
        json={
            "owner": "alice",
            "consent_id": consent["consent_id"],
            "reader_app": "knowbench",
            "ref": "test-5",
            "items": pins,
        },
    )
    assert grant.status_code == 201, grant.text
    assert grant.json()["kind"] == "test" and grant.json()["app"] == "knowlab"
    revoked = await client.post(f"{BASE}/test-grants/{grant.json()['grant_id']}/revoke")
    assert revoked.status_code == 204
    state["owner"] = Owner("seller", "knowmart")
    goods = (
        await client.post(f"{BASE}/items", params={"name": "b.csv"}, content=CSV)
    ).json()
    state["principal"] = service_principal("user-data:purchase")
    bought = await client.post(
        f"{BASE}/purchases",
        json={
            "seller": "seller",
            "seller_item_id": goods["item_id"],
            "buyer": "alice",
            "buyer_app": "knowbench",
            "transaction_ref": "tx-7",
        },
    )
    assert bought.status_code == 201 and bought.json()["origin"] == "purchase"
