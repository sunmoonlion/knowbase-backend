"""管理端：数据目录（F-KNOW-08 至 11；账 56 起只在管理端）。

我们看公共库里有哪些数据集、每个数据集的表、列、口径与局限。只读，只给结构。
任何一行数据、对象存储的位置都不从这里出去。用户侧没有这一页：对用户，公共数据是「问就有」。
"""

from __future__ import annotations

import logging
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Path, Query

from app.application.services.dataset_catalog import UnknownDataset
from app.application.services.dataset_pages import DatasetPages
from app.bootstrap.datasets import shared_dataset_pages
from app.interfaces.http.middleware.auth import require_knowbase_admin
from app.interfaces.schemas.catalog import CatalogDatasetRead, CatalogListRead

log = logging.getLogger(__name__)

router = APIRouter(
    prefix="/admin/v1/knowledge/catalog",
    tags=["Admin data catalog"],
    dependencies=[Depends(require_knowbase_admin)],
)


def get_dataset_pages() -> DatasetPages:
    """测试用依赖覆盖换目录。"""
    return shared_dataset_pages()


Pages = Annotated[DatasetPages, Depends(get_dataset_pages)]


@router.get("/datasets", response_model=CatalogListRead)
async def datasets(
    pages: Pages,
    q: Annotated[str | None, Query(max_length=80)] = None,
) -> dict:
    """现有的数据集，与工具 `list_datasets` 列的是同一份。`q` 按代码或名字找。"""
    try:
        return await pages.listing(q)
    except Exception:  # 登记表查不了等：内部细节不给页面
        log.exception("catalog_listing_failed")
        raise HTTPException(status_code=503, detail="数据目录暂时打不开") from None


@router.get("/datasets/{dataset}", response_model=CatalogDatasetRead)
async def dataset(
    dataset: Annotated[str, Path(max_length=80)],
    pages: Pages,
) -> dict:
    """一个数据集的摘要、表、列、口径、局限。"""
    try:
        return await pages.detail(dataset)
    except UnknownDataset:
        raise HTTPException(status_code=404, detail="没有这个数据集") from None
    except Exception:  # 文件取不到、登记表查不了等
        log.exception("catalog_detail_failed dataset=%s", dataset)
        raise HTTPException(status_code=503, detail="这个数据集暂时打不开") from None
