"""数据集目录的装配：默认数据集 + 登记表里的现行数据集。

工具（MCP）与页面（网页端、管理端）用的是同一份目录，所以在这里建，不在某一个接口面里建。
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from app.application.services.dataset_catalog import DatasetCatalog, DatasetQueries
from app.application.services.dataset_pages import DatasetPages
from app.application.services.dataset_registry_view import DatasetRegistryView
from app.infrastructure.datasets import SqliteDatasetQueries
from app.infrastructure.external.dataset_store import (
    ObjectDatasetFiles,
    ensure_dataset,
)
from app.infrastructure.repositories.dataset_registry import SqlDatasetRegistry
from app.infrastructure.semantic import SemanticDataset
from app.infrastructure.storage.postgres import get_postgres
from core.config import Settings, get_settings


@dataclass
class Datasets:
    """一套配置下的数据集：默认的那一个，和包含它的目录。"""

    settings: Settings
    default: DatasetQueries
    catalog: DatasetCatalog

    def pages(self) -> DatasetPages:
        return DatasetPages(
            self.catalog,
            registry_enabled=self.settings.knowbase_dataset_registry_enabled,
        )


class _DefaultDataset:
    """第一次用到时才取默认数据集（可能要从对象存储下载），进程内只做一次。"""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._lock = threading.Lock()
        self._ready = False

    def ensure(self) -> None:
        if self._ready:
            return
        with self._lock:
            if self._ready:
                return
            ensure_dataset(self._settings)
            self._ready = True


def build_dataset_registry() -> SqlDatasetRegistry:
    return SqlDatasetRegistry(lambda: get_postgres().session_factory)


def build_dataset_files(settings: Settings) -> ObjectDatasetFiles:
    return ObjectDatasetFiles(settings)


def build_dataset_registry_view(settings: Settings) -> DatasetRegistryView:
    """管理端看的登记表：全部版本，加上文件取回来没有。"""
    return DatasetRegistryView(build_dataset_registry(), build_dataset_files(settings))


def build_datasets(settings: Settings) -> Datasets:
    semantic = settings.knowbase_semantic_engine_enabled
    fetch = _DefaultDataset(settings)

    def open_dataset(path: Path, dataset_id: str, *, ensure=None) -> DatasetQueries:
        if not semantic:
            return SqliteDatasetQueries(path, dataset_id=dataset_id)
        return SemanticDataset(
            path,
            dataset_id=dataset_id,
            cache_dir=Path(settings.knowbase_semantic_cache_dir),
            ensure=ensure,
        )

    default = open_dataset(
        Path(settings.knowbase_dataset_path),
        settings.knowbase_dataset_id,
        ensure=fetch.ensure,
    )
    enabled = settings.knowbase_dataset_registry_enabled
    catalog = DatasetCatalog(
        default=default,
        ensure_default=fetch.ensure,
        default_title=settings.knowbase_dataset_title or settings.knowbase_dataset_id,
        registry=build_dataset_registry() if enabled else None,
        files=build_dataset_files(settings) if enabled else None,
        open_dataset=open_dataset,
        # 默认数据集是样例库，文件不进镜像：
        # 本地没有、也没配对象存储里的那一份，就是这个站点没装它
        has_default=lambda: (
            Path(settings.knowbase_dataset_path).is_file()
            or bool(settings.knowbase_dataset_object)
        ),
    )
    return Datasets(settings=settings, default=default, catalog=catalog)


@lru_cache(maxsize=1)
def shared_datasets() -> Datasets:
    """这个进程的那一份：工具与页面共用，登记表只读一遍、文件只开一次。"""
    return build_datasets(get_settings())


@lru_cache(maxsize=1)
def shared_dataset_pages() -> DatasetPages:
    return shared_datasets().pages()
