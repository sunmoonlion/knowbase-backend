"""多数据集（0008-info 段三）：登记的校验、数据集目录、文件取用、MCP、登记接口。

`fixtures/sh600009-financials.dataset.bin` 是 knowfeed 从 2026-09-27 实采的 600009 建出来的
数据集文件，原样放在这里：知识服务读得了它，就是两边的约定对上了。
不访问外网，不需要数据库。
"""

from __future__ import annotations

import hashlib
import json
import shutil
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI

from app.application.services.dataset_catalog import DatasetCatalog, UnknownDataset
from app.domain.datasets import (
    ACTIVE,
    DatasetRegistration,
    DatasetVersionConflict,
    InvalidDatasetRegistration,
    RegisteredDataset,
)
from app.domain.security import Principal
from app.infrastructure.datasets import SqliteDatasetQueries
from app.infrastructure.external.dataset_store import (
    DatasetUnavailable,
    ObjectDatasetFiles,
)
from app.infrastructure.security.service_auth import require_knowbase_ingest_service
from app.interfaces.endpoints.knowledge_routes import (
    get_dataset_registry,
    internal_router,
)
from app.interfaces.errors.exception_handlers import register_exception_handlers
from app.interfaces.mcp.knowbase_mcp import KnowledgeMcp, build_router
from core.config import Settings, get_settings

FIXTURE = Path(__file__).parent / "fixtures" / "sh600009-financials.dataset.bin"
SHA256 = hashlib.sha256(FIXTURE.read_bytes()).hexdigest()
VERSION = "sh600009-financials-39a395bfa6f16b67"
BUCKET = "development-knowfeed-originals"
KEY = f"knowfeed/securities/code=600009/datasets/{VERSION}/sh600009-financials.sqlite"
TOKEN = "tok-full-0123456789abcdef"
TOKEN_NO_LIST = "tok-nolist-0123456789abcdef"


def registration(**changes) -> DatasetRegistration:
    base = {
        "dataset_id": "sh600009-financials",
        "data_version": VERSION,
        "title": "上海机场 财务报表",
        "bucket": BUCKET,
        "object_key": KEY,
        "sha256": SHA256,
        "size_bytes": FIXTURE.stat().st_size,
        "start_date": "1994-12-31",
        "end_date": "2026-06-30",
        "source_app": "knowfeed",
        "source_ref": "ingestion-1",
        "security_code": "600009",
    }
    return DatasetRegistration(**(base | changes))


def registered(
    reg: DatasetRegistration | None = None, *, by: str = "service:knowfeed-backend"
) -> RegisteredDataset:
    reg = reg or registration()
    return RegisteredDataset(
        dataset_id=reg.dataset_id,
        data_version=reg.data_version,
        title=reg.title,
        bucket=reg.bucket,
        object_key=reg.object_key,
        object_version_id=reg.object_version_id,
        sha256=reg.sha256,
        size_bytes=reg.size_bytes,
        start_date=reg.start_date,
        end_date=reg.end_date,
        security_code=reg.security_code,
        source_app=reg.source_app,
        source_ref=reg.source_ref,
        status=ACTIVE,
        registered_by=by,
        registered_at=datetime(2026, 9, 27, tzinfo=UTC),
    )


class MemoryRegistry:
    def __init__(self, *rows: RegisteredDataset) -> None:
        self.rows = {r.dataset_id: r for r in rows}
        self.calls = 0
        self.registered: list[tuple[DatasetRegistration, str]] = []

    async def register(self, reg: DatasetRegistration, *, registered_by: str):
        existing = self.rows.get(reg.dataset_id)
        if (
            existing is not None
            and existing.data_version == reg.data_version
            and existing.sha256 != reg.sha256
        ):
            raise DatasetVersionConflict("already registered with different content")
        self.registered.append((reg, registered_by))
        self.rows[reg.dataset_id] = registered(reg, by=registered_by)
        return self.rows[reg.dataset_id]

    async def active(self):
        self.calls += 1
        return list(self.rows.values())

    async def get_active(self, dataset_id: str):
        return self.rows.get(dataset_id)


class LocalFiles:
    """把夹具当作已经取回的文件。"""

    def __init__(self, directory: Path, content: bytes | None = None) -> None:
        self.directory = directory
        self.content = content if content is not None else FIXTURE.read_bytes()
        self.ensured: list[str] = []

    def ensure(self, dataset: RegisteredDataset) -> Path:
        self.ensured.append(dataset.data_version)
        path = self.directory / f"{dataset.sha256}.sqlite"
        if not path.exists():
            path.write_bytes(self.content)
        return path


@pytest.fixture
def default_dataset(tmp_path: Path) -> Path:
    path = tmp_path / "default.sqlite"
    with sqlite3.connect(path) as c:
        c.executescript(
            """
            CREATE TABLE dataset_metadata(key TEXT PRIMARY KEY, value TEXT);
            INSERT INTO dataset_metadata VALUES ('data_snapshot_id','retail-v1'),
              ('start_date','2024-01-01'),('end_date','2025-12-31');
            CREATE TABLE metric_dictionary(metric_name TEXT, display_name TEXT,
              source_table TEXT, expression_hint TEXT, unit TEXT, time_basis TEXT,
              description TEXT);
            INSERT INTO metric_dictionary VALUES ('net_revenue_cents','净营收',
              'order_performance','SUM(net_revenue_cents)','分','下单日','扣退款后');
            CREATE TABLE order_performance(order_id INTEGER PRIMARY KEY,
              amount INTEGER);
            INSERT INTO order_performance VALUES (1, 100), (2, 250);
            """
        )
    return path


def settings_for(default_dataset: Path, tmp_path: Path, **changes) -> Settings:
    base = {
        "_env_file": None,
        "knowbase_dataset_path": str(default_dataset),
        "knowbase_dataset_id": "retail",
        "knowbase_mcp_tokens_json": json.dumps(
            {
                TOKEN: {"user": "u1", "sandbox": "sb1"},
                TOKEN_NO_LIST: {
                    "user": "u2",
                    "sandbox": "sb2",
                    "tools": ["describe_schema", "run_sql"],
                },
            }
        ),
        "knowbase_dataset_registry_enabled": True,
        "knowbase_dataset_allowed_buckets": BUCKET,
        "knowbase_dataset_cache_dir": str(tmp_path / "cache"),
        "S3_ENDPOINT": "http://minio.test:9000",
        "S3_ACCESS_KEY_ID": "access",
        "S3_SECRET_ACCESS_KEY": "secret",
        "S3_FORCE_PATH_STYLE": True,
    }
    return Settings(**(base | changes))


def catalog_for(
    settings: Settings, registry, files, clock=None, **more
) -> DatasetCatalog:
    default = SqliteDatasetQueries(
        Path(settings.knowbase_dataset_path), dataset_id=settings.knowbase_dataset_id
    )
    extra = {"monotonic": clock} if clock else {}
    return DatasetCatalog(
        default=default,
        ensure_default=lambda: None,
        default_title="零售经营库",
        registry=registry,
        files=files,
        open_dataset=lambda path, dataset_id: SqliteDatasetQueries(
            path, dataset_id=dataset_id
        ),
        **extra,
        **more,
    )


# ---------------------------------------------------------------- 登记的校验


def test_a_well_formed_registration_is_accepted():
    registration().validate(allowed_buckets=frozenset({BUCKET}))


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"dataset_id": "SH600009"}, "dataset_id"),
        ({"dataset_id": "a"}, "dataset_id"),
        ({"dataset_id": "sh600009/financials"}, "dataset_id"),
        ({"data_version": "other-dataset-0001"}, "must start with the dataset_id"),
        ({"data_version": "sh600009-financials-a b"}, "data_version"),
        ({"title": "  "}, "title"),
        ({"sha256": "A" * 64}, "sha256"),
        ({"sha256": "ab" * 31}, "sha256"),
        ({"size_bytes": 0}, "size_bytes"),
        ({"size_bytes": 600 * 1024 * 1024}, "size_bytes"),
        ({"bucket": "someone-elses-bucket"}, "not allowed"),
        ({"bucket": "Bad_Bucket"}, "bucket is invalid"),
        ({"object_key": "/absolute/key.sqlite"}, "object_key"),
        ({"object_key": "a/../../etc/passwd"}, "object_key"),
        ({"object_key": "a//b.sqlite"}, "object_key"),
        ({"object_key": "a/b c.sqlite"}, "object_key"),
        ({"object_key": "a\\b.sqlite"}, "object_key"),
        ({"object_key": ""}, "object_key"),
        ({"start_date": "2026-06-30", "end_date": "1994-12-31"}, "start_date"),
        ({"start_date": "19941231"}, "start_date"),
        ({"source_app": ""}, "source_app"),
        ({"security_code": "SH6000"}, "security_code"),
    ],
)
def test_a_malformed_registration_is_refused(changes, message):
    with pytest.raises(InvalidDatasetRegistration, match=message):
        registration(**changes).validate(allowed_buckets=frozenset({BUCKET}))


def test_no_bucket_is_allowed_unless_configured():
    with pytest.raises(InvalidDatasetRegistration, match="not allowed"):
        registration().validate(allowed_buckets=frozenset())


# ---------------------------------------------------------------- 数据集目录


async def test_without_a_registry_only_the_default_dataset_exists(
    default_dataset, tmp_path
):
    settings = settings_for(default_dataset, tmp_path)
    catalog = catalog_for(settings, None, None)
    assert (await catalog.resolve(None)).dataset_id == "retail"
    assert (await catalog.resolve("retail")).dataset_id == "retail"
    listed = await catalog.describe()
    assert [d["dataset"] for d in listed] == ["retail"]
    assert listed[0]["default"] is True and listed[0]["data_version"] == "retail-v1"
    with pytest.raises(UnknownDataset, match="list_datasets"):
        await catalog.resolve("sh600009-financials")


async def test_a_registered_dataset_is_resolved_and_listed(default_dataset, tmp_path):
    settings = settings_for(default_dataset, tmp_path)
    files = LocalFiles(tmp_path)
    catalog = catalog_for(settings, MemoryRegistry(registered()), files)
    service = await catalog.resolve("sh600009-financials")
    info = service.info()
    assert (info.dataset_id, info.data_version) == ("sh600009-financials", VERSION)
    assert (info.start_date, info.end_date) == ("1994-12-31", "2026-06-30")
    assert await catalog.resolve("sh600009-financials") is service  # 同一版本只开一次
    assert files.ensured == [VERSION]
    listed = await catalog.describe()
    assert listed[1] == {
        "dataset": "sh600009-financials",
        "title": "上海机场 财务报表",
        "security_code": "600009",
        "data_version": VERSION,
        "start_date": "1994-12-31",
        "end_date": "2026-06-30",
        "default": False,
    }


@pytest.mark.parametrize("bad", ["", 123, ["sh600009-financials"], "no-such-dataset"])
async def test_unknown_or_malformed_dataset_names(default_dataset, tmp_path, bad):
    settings = settings_for(default_dataset, tmp_path)
    catalog = catalog_for(settings, MemoryRegistry(registered()), LocalFiles(tmp_path))
    with pytest.raises(UnknownDataset):
        await catalog.resolve(bad)


async def test_new_registrations_appear_after_the_cache_expires(
    default_dataset, tmp_path
):
    now = [1000.0]
    registry = MemoryRegistry()
    settings = settings_for(default_dataset, tmp_path)
    catalog = catalog_for(settings, registry, LocalFiles(tmp_path), lambda: now[0])
    assert [d["dataset"] for d in await catalog.describe()] == ["retail"]
    registry.rows["sh600009-financials"] = registered()
    assert [d["dataset"] for d in await catalog.describe()] == ["retail"]
    assert registry.calls == 1  # 三十秒内不重复查登记表
    now[0] += 31
    assert [d["dataset"] for d in await catalog.describe()] == [
        "retail",
        "sh600009-financials",
    ]


async def test_a_newer_version_replaces_the_older_one(default_dataset, tmp_path):
    now = [0.0]
    registry = MemoryRegistry(registered())
    files = LocalFiles(tmp_path)
    settings = settings_for(default_dataset, tmp_path)
    catalog = catalog_for(settings, registry, files, lambda: now[0])
    old = await catalog.resolve("sh600009-financials")
    newer = registered(
        registration(
            data_version="sh600009-financials-ffffffffffffffff", sha256="f" * 64
        )
    )
    registry.rows["sh600009-financials"] = newer
    now[0] += 31
    fresh = await catalog.resolve("sh600009-financials")
    assert fresh is not old
    assert files.ensured == [VERSION, "sh600009-financials-ffffffffffffffff"]


async def test_a_registered_dataset_cannot_shadow_the_default(
    default_dataset, tmp_path
):
    settings = settings_for(default_dataset, tmp_path)
    shadow = registered(registration(dataset_id="retail", data_version="retail-evil-1"))
    catalog = catalog_for(settings, MemoryRegistry(shadow), LocalFiles(tmp_path))
    assert (await catalog.resolve("retail")).info().data_version == "retail-v1"
    assert [d["dataset"] for d in await catalog.describe()] == ["retail"]


def test_registry_and_files_come_together(default_dataset, tmp_path):
    settings = settings_for(default_dataset, tmp_path)
    with pytest.raises(ValueError):
        catalog_for(settings, MemoryRegistry(), None)


# ---------------------------------------------------------------- 文件取用


def store(content: bytes, seen: list[httpx.Request], status: int = 200):
    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(status, content=content)

    return httpx.MockTransport(handler)


def test_file_is_fetched_once_and_kept_under_its_checksum(default_dataset, tmp_path):
    seen: list[httpx.Request] = []
    settings = settings_for(default_dataset, tmp_path)
    files = ObjectDatasetFiles(settings, store(FIXTURE.read_bytes(), seen))
    path = files.ensure(registered())
    assert path == tmp_path / "cache" / f"{SHA256}.sqlite"
    assert hashlib.sha256(path.read_bytes()).hexdigest() == SHA256
    assert files.ensure(registered()) == path and len(seen) == 1
    request = seen[0]
    # 线上发出的路径与签名用的路径是同一个写法（等号转义）
    assert request.url.raw_path.decode() == f"/{BUCKET}/{KEY}".replace("=", "%3D")
    assert request.headers["authorization"].startswith("AWS4-HMAC-SHA256 ")
    assert "secret" not in str(request.headers)


def test_a_pinned_object_version_is_requested(default_dataset, tmp_path):
    seen: list[httpx.Request] = []
    settings = settings_for(default_dataset, tmp_path)
    files = ObjectDatasetFiles(settings, store(FIXTURE.read_bytes(), seen))
    files.ensure(registered(registration(object_version_id="v-42")))
    assert seen[0].url.params["versionId"] == "v-42"


def test_content_that_does_not_match_is_not_kept(default_dataset, tmp_path):
    seen: list[httpx.Request] = []
    settings = settings_for(default_dataset, tmp_path)
    files = ObjectDatasetFiles(settings, store(b"something else", seen))
    with pytest.raises(DatasetUnavailable, match="does not match the pinned sha256"):
        files.ensure(registered())
    assert list((tmp_path / "cache").glob("*")) == []


def test_a_damaged_cache_file_is_fetched_again(default_dataset, tmp_path):
    seen: list[httpx.Request] = []
    settings = settings_for(default_dataset, tmp_path)
    files = ObjectDatasetFiles(settings, store(FIXTURE.read_bytes(), seen))
    path = files.ensure(registered())
    path.write_bytes(b"tampered")
    assert files.ensure(registered()) == path and len(seen) == 2
    assert hashlib.sha256(path.read_bytes()).hexdigest() == SHA256


@pytest.mark.parametrize(
    ("changes", "overrides", "message"),
    [
        ({"bucket": "another-bucket"}, {}, "bucket is not allowed"),
        ({}, {"knowbase_dataset_allowed_buckets": ""}, "bucket is not allowed"),
        ({}, {"S3_ACCESS_KEY_ID": None}, "S3 credentials are not configured"),
        ({"object_key": "a/../b"}, {}, "object key is invalid"),
    ],
)
def test_files_that_may_not_be_fetched(
    default_dataset, tmp_path, changes, overrides, message
):
    seen: list[httpx.Request] = []
    settings = settings_for(default_dataset, tmp_path, **overrides)
    files = ObjectDatasetFiles(settings, store(FIXTURE.read_bytes(), seen))
    with pytest.raises(DatasetUnavailable, match=message):
        files.ensure(registered(registration(**changes)))
    assert seen == []


def test_storage_errors_do_not_leave_partial_files(default_dataset, tmp_path):
    seen: list[httpx.Request] = []
    settings = settings_for(default_dataset, tmp_path)
    files = ObjectDatasetFiles(settings, store(b"denied", seen, status=403))
    with pytest.raises(DatasetUnavailable, match="HTTP 403"):
        files.ensure(registered())
    assert list((tmp_path / "cache").glob("*")) == []


def test_whether_a_file_was_fetched_is_seen_without_fetching(default_dataset, tmp_path):
    seen: list[httpx.Request] = []
    settings = settings_for(default_dataset, tmp_path)
    files = ObjectDatasetFiles(settings, store(FIXTURE.read_bytes(), seen))
    assert files.fetched(registered()) is False and seen == []
    files.ensure(registered())
    assert files.fetched(registered()) is True and len(seen) == 1


# ---------------------------------------------------------------- MCP


async def test_a_site_without_the_default_dataset_lists_only_what_is_registered(
    default_dataset, tmp_path
):
    """新集群上的样子：样例库没装，登记进来的数据集照常能列、能查。"""
    settings = settings_for(default_dataset, tmp_path)
    touched = []
    catalog = catalog_for(
        settings,
        MemoryRegistry(registered()),
        LocalFiles(tmp_path),
        has_default=lambda: False,
    )
    catalog._ensure_default = lambda: touched.append("default")
    listed = await catalog.describe()
    assert [(d["dataset"], d["default"]) for d in listed] == [
        ("sh600009-financials", False)
    ]
    assert (await catalog.resolve("sh600009-financials")).dataset_id == (
        "sh600009-financials"
    )
    for name in (None, "retail"):
        with pytest.raises(UnknownDataset, match="no default dataset.*list_datasets"):
            await catalog.resolve(name)
    assert touched == []  # 没装就不去取


async def test_a_site_with_neither_default_nor_registered_datasets_has_an_empty_list(
    default_dataset, tmp_path
):
    settings = settings_for(default_dataset, tmp_path)
    catalog = catalog_for(settings, None, None, has_default=lambda: False)
    assert await catalog.describe() == []


async def test_without_the_default_the_tools_say_where_to_look(
    default_dataset, tmp_path
):
    settings = settings_for(default_dataset, tmp_path)
    catalog = catalog_for(
        settings,
        MemoryRegistry(registered()),
        LocalFiles(tmp_path),
        has_default=lambda: False,
    )
    app = FastAPI()
    register_exception_handlers(app)
    app.include_router(build_router(settings, catalog), prefix="/api")
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://knowbase-tls.app-platform-dev.svc.cluster.local",
    ) as client:
        error, data, _ = await call(client, "list_datasets")
        assert not error
        assert [d["dataset"] for d in data["datasets"]] == ["sh600009-financials"]
        error, _, text = await call(client, "run_sql", {"sql": "SELECT 1"})
        assert error and "list_datasets" in text and "unavailable" not in text
        error, data, _ = await call(
            client, "describe_schema", {"dataset": "sh600009-financials"}
        )
        assert not error and data["tables"]


def test_the_default_is_installed_only_when_its_file_or_object_is_there(tmp_path):
    from app.bootstrap.datasets import build_datasets

    def site(**changes):
        base = {
            "_env_file": None,
            "knowbase_dataset_path": str(tmp_path / "absent.sqlite"),
            "knowbase_semantic_engine_enabled": False,
        }
        return build_datasets(Settings(**(base | changes))).catalog

    assert site()._has_default() is False
    assert site(knowbase_dataset_object="s3://b/k.sqlite")._has_default() is True
    present = tmp_path / "here.sqlite"
    present.write_bytes(b"x")
    assert site(knowbase_dataset_path=str(present))._has_default() is True


@pytest.fixture
async def mcp(default_dataset, tmp_path):
    settings = settings_for(default_dataset, tmp_path)
    files = LocalFiles(tmp_path)
    catalog = catalog_for(settings, MemoryRegistry(registered()), files)
    app = FastAPI()
    register_exception_handlers(app)
    app.include_router(build_router(settings, catalog), prefix="/api")
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://knowbase-tls.app-platform-dev.svc.cluster.local",
    ) as client:
        yield client


async def call(client, name: str, arguments: dict | None = None, token: str = TOKEN):
    response = await client.post(
        "/api/mcp/knowledge",
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": name, "arguments": arguments or {}},
        },
        headers={"Authorization": f"Bearer {token}"},
    )
    assert response.status_code == 200
    result = response.json()["result"]
    return (
        result["isError"],
        result.get("structuredContent"),
        result["content"][0]["text"],
    )


async def test_list_datasets_shows_the_default_and_the_registered(mcp):
    error, data, _ = await call(mcp, "list_datasets")
    assert not error
    assert [(d["dataset"], d["default"]) for d in data["datasets"]] == [
        ("retail", True),
        ("sh600009-financials", False),
    ]
    assert data["datasets"][1]["security_code"] == "600009"


async def test_tools_without_dataset_use_the_default(mcp):
    error, data, _ = await call(
        mcp, "run_sql", {"sql": "SELECT SUM(amount) AS total FROM order_performance"}
    )
    assert not error and data["rows"] == [{"total": 350}]
    assert data["citation"]["dataset_id"] == "retail"
    assert data["citation"]["data_version"] == "retail-v1"


async def test_the_same_tools_query_the_registered_dataset(mcp):
    dataset = {"dataset": "sh600009-financials"}
    error, data, _ = await call(
        mcp,
        "run_sql",
        dataset
        | {
            "sql": "SELECT fiscal_year, basis, operate_income FROM income_statement "
            "WHERE report_type='年报' AND fiscal_year BETWEEN 2020 AND 2022 "
            "ORDER BY fiscal_year"
        },
    )
    assert not error
    assert data["rows"] == [
        {"fiscal_year": 2020, "basis": "原始披露", "operate_income": 4303465087.94},
        {"fiscal_year": 2021, "basis": "追溯调整后", "operate_income": 8154776878.02},
        {"fiscal_year": 2022, "basis": "原始披露", "operate_income": 5480447621.36},
    ]
    assert data["citation"]["dataset_id"] == "sh600009-financials"
    assert data["citation"]["data_version"] == VERSION
    assert data["citation"]["as_of"] == "2026-06-30"
    error, data, _ = await call(
        mcp, "metric_definitions", dataset | {"metric": "毛利率"}
    )
    assert not error and [m["metric_name"] for m in data["metrics"]] == ["gross_margin"]
    error, data, _ = await call(mcp, "describe_schema", dataset)
    assert not error and {t["name"] for t in data["tables"]} >= {
        "balance_sheet",
        "income_statement",
        "cash_flow",
        "official_key_figures",
        "disclosure_calendar",
        "reconciliation_rules",
    }


async def test_the_registered_dataset_keeps_the_read_only_guard(mcp):
    for sql in ("DELETE FROM income_statement", "SELECT 1; SELECT 2", "PRAGMA x"):
        error, _, text = await call(
            mcp, "run_sql", {"dataset": "sh600009-financials", "sql": sql}
        )
        assert error and "allowed" in text


async def test_an_unknown_dataset_is_a_tool_error_that_points_to_the_list(mcp):
    error, _, text = await call(
        mcp, "run_sql", {"dataset": "sh600519-financials", "sql": "SELECT 1"}
    )
    assert error and "unknown dataset" in text and "list_datasets" in text


async def test_a_token_without_list_datasets_cannot_list_but_can_query(mcp):
    error, _, text = await call(mcp, "list_datasets", token=TOKEN_NO_LIST)
    assert error and "not allowed" in text
    error, data, _ = await call(
        mcp,
        "run_sql",
        {
            "dataset": "sh600009-financials",
            "sql": "SELECT COUNT(*) AS n FROM cash_flow",
        },
        token=TOKEN_NO_LIST,
    )
    assert not error and data["rows"] == [{"n": 103}]


async def test_a_registry_failure_is_not_shown_to_the_model(default_dataset, tmp_path):
    class Broken(MemoryRegistry):
        async def active(self):
            raise RuntimeError("connection to 10.0.0.5:5432 refused, password=hunter2")

    settings = settings_for(default_dataset, tmp_path)
    server = KnowledgeMcp(
        settings, catalog_for(settings, Broken(), LocalFiles(tmp_path))
    )
    grant = server.tokens.resolve(f"Bearer {TOKEN}")
    reply = await server.handle(
        grant,
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "list_datasets", "arguments": {}},
        },
    )
    text = reply["result"]["content"][0]["text"]
    assert reply["result"]["isError"] and text == (
        "dataset service is temporarily unavailable"
    )


async def test_registry_is_off_by_default(default_dataset, tmp_path):
    settings = settings_for(
        default_dataset, tmp_path, knowbase_dataset_registry_enabled=False
    )
    server = KnowledgeMcp(settings)
    grant = server.tokens.resolve(f"Bearer {TOKEN}")
    reply = await server.handle(
        grant,
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "list_datasets", "arguments": {}},
        },
    )
    listed = reply["result"]["structuredContent"]["datasets"]
    assert [d["dataset"] for d in listed] == ["retail"]


# ---------------------------------------------------------------- 登记接口


def principal() -> Principal:
    return Principal(
        actor_type="service",
        subject="service:knowfeed-backend",
        issuer="https://casdoor.test",
        app="knowfeed",
        surface="internal",
        audience="knowbase",
        scopes=frozenset({"knowbase:ingest"}),
        authenticated_at=datetime(2026, 9, 27, tzinfo=UTC),
        expires_at=datetime(2026, 9, 27, 1, tzinfo=UTC),
        policy_version="test",
    )


def body(**changes) -> dict:
    base = {
        "dataset_id": "sh600009-financials",
        "data_version": VERSION,
        "title": "上海机场 财务报表",
        "security_code": "600009",
        "object": f"s3://{BUCKET}/{KEY}",
        "sha256": SHA256,
        "size_bytes": FIXTURE.stat().st_size,
        "start_date": "1994-12-31",
        "end_date": "2026-06-30",
        "source_app": "knowfeed",
        "source_ref": "ingestion-1",
        "quality_passed": True,
    }
    return base | changes


@pytest.fixture
async def api(default_dataset, tmp_path):
    registry = MemoryRegistry()
    state = {"settings": settings_for(default_dataset, tmp_path), "authenticated": True}
    app = FastAPI()
    register_exception_handlers(app)
    app.include_router(internal_router, prefix="/api")

    def authenticate() -> Principal:
        if not state["authenticated"]:
            from fastapi import HTTPException

            raise HTTPException(status_code=401, detail="unauthorized")
        return principal()

    app.dependency_overrides[require_knowbase_ingest_service] = authenticate
    app.dependency_overrides[get_dataset_registry] = lambda: registry
    app.dependency_overrides[get_settings] = lambda: state["settings"]
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://knowbase-tls.app-platform-dev.svc.cluster.local",
    ) as client:
        yield client, registry, state


URL = "/api/internal/v1/knowledge/datasets"


async def test_registration_records_who_registered_from_the_verified_identity(api):
    client, registry, _ = api
    response = await client.post(URL, json=body())
    assert response.status_code == 201
    assert response.json()["status"] == "active"
    assert response.json()["registered_by"] == "service:knowfeed-backend"
    reg, who = registry.registered[0]
    assert who == "service:knowfeed-backend"
    assert (reg.bucket, reg.object_key) == (BUCKET, KEY)
    listed = (await client.get(URL)).json()
    assert [d["data_version"] for d in listed] == [VERSION]
    assert "object_key" not in listed[0] and "bucket" not in listed[0]


async def test_registration_requires_the_service_identity(api):
    client, registry, state = api
    state["authenticated"] = False
    assert (await client.post(URL, json=body())).status_code == 401
    assert (await client.get(URL)).status_code == 401
    assert registry.registered == []


@pytest.mark.parametrize(
    ("changes", "status"),
    [
        ({"quality_passed": False}, 422),
        ({"object": "https://example.com/file.sqlite"}, 422),
        ({"object": f"s3://other-bucket/{KEY}"}, 422),
        ({"object": f"s3://{BUCKET}/a/../b.sqlite"}, 422),
        ({"sha256": "z" * 64}, 422),
        ({"dataset_id": "retail", "data_version": "retail-0001"}, 422),
        ({"data_version": "another-0001"}, 422),
        ({"registered_by": "service:someone"}, 422),
        ({"size_bytes": -1}, 422),
    ],
)
async def test_registrations_that_are_refused(api, changes, status):
    client, registry, _ = api
    response = await client.post(URL, json=body(**changes))
    assert response.status_code == status, response.text
    assert registry.registered == []


async def test_the_same_version_with_other_content_is_a_conflict(api):
    client, _, _ = api
    assert (await client.post(URL, json=body())).status_code == 201
    response = await client.post(URL, json=body(sha256="a" * 64))
    assert response.status_code == 409


async def test_the_interface_does_not_exist_while_the_registry_is_off(
    api, default_dataset, tmp_path
):
    client, registry, state = api
    state["settings"] = settings_for(
        default_dataset, tmp_path, knowbase_dataset_registry_enabled=False
    )
    assert (await client.post(URL, json=body())).status_code == 404
    assert (await client.get(URL)).status_code == 404
    assert registry.registered == []


def test_the_fixture_is_the_file_info_built():
    assert FIXTURE.stat().st_size == 143360
    copy = Path(shutil.copy(FIXTURE, FIXTURE.with_suffix(".check")))
    try:
        with sqlite3.connect(copy) as c:
            version = c.execute(
                "select value from dataset_metadata where key='data_snapshot_id'"
            ).fetchone()[0]
    finally:
        copy.unlink()
    assert version == VERSION
