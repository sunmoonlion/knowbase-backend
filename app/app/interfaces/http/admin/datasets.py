"""管理端：数据集登记表（F-KNOW-12）。

只读。登记由 knowfeed 发起，回退由 knowfeed 的「重新登记」做，这里不提供改的动作。
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Path

from app.application.services.dataset_registry_view import DatasetRegistryView
from app.bootstrap.datasets import build_dataset_registry_view
from app.interfaces.http.middleware.auth import require_knowbase_admin
from app.interfaces.schemas.catalog import DatasetVersionsRead, RegistryRead
from core.config import Settings, get_settings

router = APIRouter(
    prefix="/admin/v1/knowledge/datasets",
    tags=["Admin dataset registry"],
    dependencies=[Depends(require_knowbase_admin)],
)


def registry_settings() -> Settings:
    """测试用依赖覆盖换配置。"""
    return get_settings()


def get_registry_view(
    settings: Annotated[Settings, Depends(registry_settings)],
) -> DatasetRegistryView:
    return build_dataset_registry_view(settings)


Config = Annotated[Settings, Depends(registry_settings)]
View = Annotated[DatasetRegistryView, Depends(get_registry_view)]


@router.get("", response_model=RegistryRead)
async def registered_datasets(settings: Config, view: View) -> dict:
    """每个登记的数据集和它的现行版本。多数据集没打开时是空的。"""
    if not settings.knowbase_dataset_registry_enabled:
        return {"enabled": False, "datasets": []}
    return {"enabled": True, "datasets": await view.datasets()}


@router.get("/{dataset}/versions", response_model=DatasetVersionsRead)
async def dataset_versions(
    dataset: Annotated[str, Path(max_length=80)], settings: Config, view: View
) -> dict:
    """一个数据集的各个版本：哪个是现行的，哪些被取代了、什么时候。"""
    if not settings.knowbase_dataset_registry_enabled:
        raise HTTPException(status_code=404, detail="没有这个数据集")
    versions = await view.versions(dataset)
    if not versions:
        raise HTTPException(status_code=404, detail="没有这个数据集")
    return {"dataset_id": dataset, "versions": versions}
