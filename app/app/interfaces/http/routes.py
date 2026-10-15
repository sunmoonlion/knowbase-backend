from fastapi import APIRouter, Depends

from app.bootstrap.datasets import shared_datasets
from app.interfaces.endpoints.knowledge_routes import (
    internal_router as knowledge_internal_router,
)
from app.interfaces.endpoints.knowledge_routes import (
    router as knowledge_admin_router,
)
from app.interfaces.http.admin.auth import router as admin_auth_router
from app.interfaces.http.admin.catalog import router as admin_catalog_router
from app.interfaces.http.admin.datasets import router as admin_datasets_router
from app.interfaces.http.admin.diagnostics import router as admin_diagnostics_router
from app.interfaces.http.internal.delivery_metrics import (
    router as delivery_metrics_router,
)
from app.interfaces.http.middleware.auth import require_knowbase_admin
from app.interfaces.http.web.auth import router as web_auth_router
from app.interfaces.http.web.cross_app import router as web_cross_app_router
from app.interfaces.http.web.interactions import router as web_interactions_router
from app.interfaces.mcp.knowbase_mcp import build_router as build_knowledge_mcp_router

router = APIRouter()
router.include_router(admin_auth_router)
router.include_router(web_auth_router)
router.include_router(admin_diagnostics_router)
router.include_router(delivery_metrics_router)
router.include_router(web_interactions_router)
router.include_router(web_cross_app_router)
router.include_router(admin_catalog_router)
router.include_router(admin_datasets_router)
router.include_router(
    knowledge_admin_router,
    dependencies=[Depends(require_knowbase_admin)],
)
router.include_router(knowledge_internal_router)
# 工具与数据目录页面用同一份数据集目录（F-KNOW-11）
router.include_router(build_knowledge_mcp_router(catalog=shared_datasets().catalog))
