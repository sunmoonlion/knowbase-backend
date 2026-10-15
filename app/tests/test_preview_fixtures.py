"""给管理端造样例（knowbase-admin-frontend `preview/fixtures/`）。

账 56 起数据目录只在管理端，用户侧没有页面，所以样例都是管理接口的返回。
数据目录的返回由真的目录、真的查询、真的接口造出来：上海机场那一个是 knowfeed 实采建出来的
数据集文件（`fixtures/sh600009-financials-v2.dataset.bin`）；贵州茅台那一个是这里现编的，
它自己的说明里写明了是样例。目录页只有结构，所以样例里没有任何数值。
平时它就是一个测试。要把样例写进管理端的仓库：

    PREVIEW_ADMIN_FIXTURES_OUT=<管理端>/app/preview/fixtures \\
        uv run pytest tests/test_preview_fixtures.py

登记表那一份要真数据库（`DELIVERY_TEST_DATABASE_URL`），没有就跳过那一个。
情景：`catalog`、`catalog-default-only` 是目录；`full`、`off` 是登记表。
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
from fastapi import FastAPI
from preview_recorder import Recorder
from test_auth_routes_security import session
from test_durable_delivery_db import db as db  # noqa: F401
from test_knowledge_datasets import BUCKET, MemoryRegistry, registered, registration
from test_knowledge_datasets import default_dataset as default_dataset  # noqa: F401

from app.application.services.dataset_catalog import DatasetCatalog
from app.application.services.dataset_pages import DatasetPages
from app.application.services.dataset_registry_view import DatasetRegistryView
from app.infrastructure.datasets import SqliteDatasetQueries
from app.infrastructure.external.dataset_store import ObjectDatasetFiles
from app.infrastructure.repositories.dataset_registry import SqlDatasetRegistry
from app.interfaces.http.admin import catalog as catalog_routes
from app.interfaces.http.admin import datasets as admin_routes
from app.interfaces.http.middleware.auth import require_knowbase_admin
from core.config import Settings

HERE = Path(__file__).parent
AIRPORT_FILE = HERE / "fixtures" / "sh600009-financials-v2.dataset.bin"
AIRPORT_OLD_FILE = HERE / "fixtures" / "sh600009-financials.dataset.bin"
AIRPORT = "sh600009-financials"
AIRPORT_VERSION = "sh600009-financials-9fd91db79529e208"
AIRPORT_OLD_VERSION = "sh600009-financials-39a395bfa6f16b67"
MOUTAI = "sh600519-financials"
MOUTAI_VERSION = "sh600519-financials-0000000000000001"
SAMPLE_NOTE = "这是给预览用的样例数据集：表、列与口径是编的，不是真的入库结果"


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def made_up_dataset(path: Path) -> Path:
    """现编的一个数据集：让目录里不只一家公司。它的说明里写明是样例。"""
    with sqlite3.connect(path) as c:
        c.executescript(
            f"""
            CREATE TABLE dataset_metadata(key TEXT PRIMARY KEY, value TEXT);
            INSERT INTO dataset_metadata VALUES
              ('data_snapshot_id','{MOUTAI_VERSION}'),
              ('security_code','600519'),
              ('start_date','2001-12-31'),('end_date','2026-06-30'),
              ('sample_note','{SAMPLE_NOTE}'),
              ('unit_note','金额单位为元');
            CREATE TABLE income_statement(security_code TEXT, report_date TEXT,
              report_type TEXT, operate_income REAL, operate_cost REAL,
              netprofit REAL);
            CREATE TABLE field_dictionary(source_table TEXT, field TEXT,
              display_name TEXT, unit TEXT);
            INSERT INTO field_dictionary VALUES
              ('income_statement','security_code','证券代码',NULL),
              ('income_statement','report_date','报告期末日',NULL),
              ('income_statement','report_type','报告类型',NULL),
              ('income_statement','operate_income','营业收入','元'),
              ('income_statement','operate_cost','营业成本','元'),
              ('income_statement','netprofit','净利润','元');
            CREATE TABLE metric_dictionary(metric_name TEXT, display_name TEXT,
              source_table TEXT, expression_hint TEXT, unit TEXT, time_basis TEXT,
              description TEXT);
            INSERT INTO metric_dictionary VALUES ('gross_margin','毛利率',
              'income_statement','(operate_income - operate_cost) / operate_income',
              '比率','报告期','营业收入减营业成本，除以营业收入');
            """
        )
    return path


class Files:
    """把本机的文件当作已经取回的文件。"""

    def __init__(self, paths: dict[str, Path]) -> None:
        self.paths = paths

    def ensure(self, dataset) -> Path:
        return self.paths[dataset.dataset_id]


def airport(**changes):
    base = {
        "data_version": AIRPORT_VERSION,
        "sha256": sha(AIRPORT_FILE),
        "size_bytes": AIRPORT_FILE.stat().st_size,
        "object_key": (
            f"knowfeed/securities/code=600009/datasets/{AIRPORT_VERSION}/"
            "sh600009-financials.sqlite"
        ),
    }
    return registration(**(base | changes))


def moutai(path: Path):
    return registration(
        dataset_id=MOUTAI,
        data_version=MOUTAI_VERSION,
        title="贵州茅台 财务报表（样例）",
        security_code="600519",
        sha256=sha(path),
        size_bytes=path.stat().st_size,
        start_date="2001-12-31",
        object_key=(
            f"knowfeed/securities/code=600519/datasets/{MOUTAI_VERSION}/"
            "sh600519-financials.sqlite"
        ),
    )


def settings_of(default_dataset: Path, tmp_path: Path, *, registry: bool) -> Settings:  # noqa: F811
    return Settings(
        _env_file=None,
        knowbase_dataset_path=str(default_dataset),
        knowbase_dataset_id="retail",
        knowbase_dataset_title="零售经营库（最小实例）",
        knowbase_dataset_registry_enabled=registry,
        knowbase_dataset_allowed_buckets=BUCKET,
        knowbase_dataset_cache_dir=str(tmp_path / "cache"),
    )


def served(pages: DatasetPages) -> httpx.AsyncClient:
    app = FastAPI()
    app.include_router(catalog_routes.router, prefix="/api")
    app.dependency_overrides[require_knowbase_admin] = lambda: (
        session("admin", "knowbase:admin").principal
    )
    app.dependency_overrides[catalog_routes.get_dataset_pages] = lambda: pages
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://knowbase"
    )


def pages_of(config: Settings, rows: list, files: dict[str, Path]) -> DatasetPages:
    def opened(path: Path, dataset_id: str) -> SqliteDatasetQueries:
        return SqliteDatasetQueries(path, dataset_id=dataset_id)

    enabled = config.knowbase_dataset_registry_enabled
    catalog = DatasetCatalog(
        default=opened(Path(config.knowbase_dataset_path), "retail"),
        ensure_default=lambda: None,
        default_title=config.knowbase_dataset_title or "retail",
        registry=MemoryRegistry(*rows) if enabled else None,
        files=Files(files) if enabled else None,
        open_dataset=opened if enabled else None,
    )
    return DatasetPages(catalog, registry_enabled=enabled)


def already_fetched(config: Settings, file: Path) -> None:
    """现行的上海机场已经取回来了；别的还没有人查过，所以还没取。"""
    cache = Path(config.knowbase_dataset_cache_dir)
    cache.mkdir(parents=True)
    (cache / f"{sha(file)}.sqlite").write_bytes(file.read_bytes())


LIST = "/api/admin/v1/knowledge/catalog/datasets"


async def build_full(rec: Recorder, http: httpx.AsyncClient) -> None:
    await rec.get(http, LIST)
    # 预览里搜没录过的词，前门会拿整份列表来答：这里录的是说明用的几个词
    for q in ("600009", "机场", "600519", "000001", "平安银行"):
        await rec.get(http, LIST, q=q)
    for dataset in ("retail", AIRPORT, MOUTAI):
        await rec.get(http, f"{LIST}/{dataset}")
    await rec.get(http, f"{LIST}/sh000001-financials", expect=404)
    rec.page("数据目录", "/zh-CN/knowledge/catalog")
    rec.page("搜到一家", "/zh-CN/knowledge/catalog?q=600009")
    rec.page("没有这家公司的数据", "/zh-CN/knowledge/catalog?q=000001")
    rec.page("一个数据集：上海机场（实采的）", f"/zh-CN/knowledge/catalog/{AIRPORT}")
    rec.page("一个数据集：现编的样例", f"/zh-CN/knowledge/catalog/{MOUTAI}")
    rec.page("默认数据集", "/zh-CN/knowledge/catalog/retail")
    rec.page("没有这个数据集", "/zh-CN/knowledge/catalog/sh000001-financials")


async def build_default_only(rec: Recorder, http: httpx.AsyncClient) -> None:
    await rec.get(http, LIST)
    await rec.get(http, LIST, q="600009")
    await rec.get(http, f"{LIST}/retail")
    rec.page("数据目录（只有默认数据集）", "/zh-CN/knowledge/catalog")
    rec.page("搜一家公司：没有", "/zh-CN/knowledge/catalog?q=600009")
    rec.page("默认数据集", "/zh-CN/knowledge/catalog/retail")


def where_to(tmp_path: Path) -> Path:
    wanted = os.environ.get("PREVIEW_ADMIN_FIXTURES_OUT")
    return Path(wanted) if wanted else tmp_path / "admin"


def written(directory: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    manifest = json.loads((directory / "manifest.json").read_text())
    bodies = {
        f"{r['path']}?{r['query']}": json.loads((directory / r["file"]).read_text())
        for r in manifest["responses"]
    }
    return manifest, bodies


def numbers_in(path: Path) -> list[str]:
    """数据集里的数值：样例里一个都不该有。"""
    with sqlite3.connect(path) as c:
        rows = c.execute(
            "SELECT total_assets, total_liabilities FROM balance_sheet "
            "WHERE total_assets IS NOT NULL"
        ).fetchall()
    return [str(v) for row in rows for v in row if v is not None]


async def test_a_catalog_with_datasets(default_dataset, tmp_path):  # noqa: F811
    made_up = made_up_dataset(tmp_path / "moutai.sqlite")
    config = settings_of(default_dataset, tmp_path, registry=True)
    pages = pages_of(
        config,
        [registered(airport()), registered(moutai(made_up))],
        {AIRPORT: AIRPORT_FILE, MOUTAI: made_up},
    )
    rec = Recorder(
        "catalog",
        title="管理端：数据目录，有几个数据集",
        description=(
            "默认数据集之外登记了两家公司。上海机场的表、列、口径、说明是 knowfeed 实采"
            "建出来的那一份；贵州茅台那一个是为预览现编的，它的说明里写着。"
            "只有结构，没有任何数值。搜没录过的词，预览会拿整份列表来答。"
        ),
    )
    async with served(pages) as http:
        await build_full(rec, http)
    directory = rec.write(where_to(tmp_path))
    manifest, bodies = written(directory)

    listing = bodies[f"{LIST}?"]
    assert [d["dataset"] for d in listing["datasets"]] == ["retail", AIRPORT, MOUTAI]
    assert listing["datasets"][0]["title"] == "零售经营库（最小实例）"
    assert bodies[f"{LIST}?q=000001"]["datasets"] == []
    assert bodies[f"{LIST}?q=000001"]["total"] == 3
    assert [d["dataset"] for d in bodies[f"{LIST}?q=机场"]["datasets"]] == [AIRPORT]

    detail = bodies[f"{LIST}/{AIRPORT}?"]
    assert detail["data_version"] == AIRPORT_VERSION
    assert len(detail["metrics"]) == 10 and len(detail["limitations"]) >= 6
    assert {t["kind"] for t in detail["tables"]} == {"data", "dictionary"}
    sample = bodies[f"{LIST}/{MOUTAI}?"]
    assert SAMPLE_NOTE in [n["text"] for n in sample["limitations"]]
    assert bodies[f"{LIST}/retail?"]["default"] is True
    assert [r["status"] for r in manifest["responses"]].count(404) == 1
    assert len(manifest["pages"]) == 7

    everything = "".join(
        (directory / r["file"]).read_text() for r in manifest["responses"]
    )
    # 存放位置、校验值、数据的数值，都不在样例里
    assert BUCKET not in everything and "knowfeed/securities" not in everything
    assert sha(AIRPORT_FILE) not in everything and "object_key" not in everything
    assert not [v for v in numbers_in(AIRPORT_FILE) if v in everything]


async def test_a_catalog_with_only_the_default_dataset(default_dataset, tmp_path):  # noqa: F811
    config = settings_of(default_dataset, tmp_path, registry=False)
    rec = Recorder(
        "catalog-default-only",
        title="管理端：数据目录，只有默认数据集",
        description="多数据集没有打开，或者一家公司都还没有登记进来。",
    )
    async with served(pages_of(config, [], {})) as http:
        await build_default_only(rec, http)
    manifest, bodies = written(rec.write(where_to(tmp_path)))
    assert bodies[f"{LIST}?"]["registry_enabled"] is False
    assert [d["dataset"] for d in bodies[f"{LIST}?"]["datasets"]] == ["retail"]
    assert bodies[f"{LIST}?q=600009"] == {
        "registry_enabled": False,
        "total": 1,
        "datasets": [],
    }
    assert len(manifest["pages"]) == 3


async def test_the_registry_for_the_admin_pages(db, default_dataset, tmp_path):  # noqa: F811
    """管理端的登记表：上海机场登记过两个版本，贵州茅台一个。

    管理端没有预览服务（账本 H46），这些样例用来核对管理端手写的契约、跑组件测试。
    """
    sessions = db
    # 登记时间用固定的：重录一遍，样例不变
    moments = iter(datetime(2026, 9, day, 2, 30, tzinfo=UTC) for day in (27, 28, 30))
    registry = SqlDatasetRegistry(lambda: sessions, clock=lambda: next(moments))
    made_up = made_up_dataset(tmp_path / "moutai.sqlite")
    older = airport(
        data_version=AIRPORT_OLD_VERSION,
        sha256=sha(AIRPORT_OLD_FILE),
        size_bytes=AIRPORT_OLD_FILE.stat().st_size,
        object_key=(
            f"knowfeed/securities/code=600009/datasets/{AIRPORT_OLD_VERSION}/"
            "sh600009-financials.sqlite"
        ),
    )
    for reg in (older, moutai(made_up), airport()):
        await registry.register(reg, registered_by="service:knowfeed-backend")
    state = SimpleNamespace(
        config=settings_of(default_dataset, tmp_path, registry=True)
    )
    already_fetched(state.config, AIRPORT_FILE)

    app = FastAPI()
    app.include_router(admin_routes.router, prefix="/api")
    app.dependency_overrides[require_knowbase_admin] = lambda: (
        session("admin", "knowbase:admin").principal
    )
    app.dependency_overrides[admin_routes.registry_settings] = lambda: state.config
    app.dependency_overrides[admin_routes.get_registry_view] = lambda: (
        DatasetRegistryView(registry, ObjectDatasetFiles(state.config))
    )
    out = where_to(tmp_path)
    url = "/api/admin/v1/knowledge/datasets"
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://knowbase"
    ) as http:
        rec = Recorder(
            "full",
            title="管理端：登记表",
            description="上海机场登记过两个版本（旧的被取代），贵州茅台（样例）一个。",
        )
        await rec.get(http, url)
        await rec.get(http, f"{url}/{AIRPORT}/versions")
        await rec.get(http, f"{url}/{MOUTAI}/versions")
        await rec.get(http, f"{url}/sh000001-financials/versions", expect=404)
        rec.page("数据集登记", "/zh-CN/knowledge/datasets")
        manifest, bodies = written(rec.write(out))

        state.config = settings_of(default_dataset, tmp_path, registry=False)
        off = Recorder(
            "off",
            title="管理端：多数据集没有打开",
            description="登记表是空的，页面说明原因。",
        )
        await off.get(http, url)
        off.page("数据集登记", "/zh-CN/knowledge/datasets")
        _, off_bodies = written(off.write(out))

    listed = bodies[f"{url}?"]["datasets"]
    assert [(d["dataset_id"], d["version_count"]) for d in listed] == [
        (AIRPORT, 2),
        (MOUTAI, 1),
    ]
    assert listed[0]["current"]["fetched"] is True
    assert listed[1]["current"]["fetched"] is False
    history = bodies[f"{url}/{AIRPORT}/versions?"]["versions"]
    assert [(v["data_version"], v["status"]) for v in history] == [
        (AIRPORT_VERSION, "active"),
        (AIRPORT_OLD_VERSION, "superseded"),
    ]
    assert history[0]["superseded_at"] is None and history[1]["superseded_at"]
    assert len(manifest["responses"]) == 4
    assert off_bodies[f"{url}?"] == {"enabled": False, "datasets": []}
