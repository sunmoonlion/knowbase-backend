from fastapi import APIRouter, Depends

from app.interfaces.endpoints.knowledge_routes import (
    internal_router as knowledge_internal_router,
)
from app.interfaces.endpoints.knowledge_routes import (
    router as knowledge_admin_router,
)
from app.interfaces.endpoints.library_web_routes import router as library_web_router
from app.interfaces.endpoints.user_data_admin_routes import (
    router as user_data_admin_router,
)
from app.interfaces.endpoints.user_fact_routes import router as user_fact_router
from app.interfaces.endpoints.user_library_routes import router as user_library_router
from app.interfaces.endpoints.user_resolution_routes import (
    router as user_resolution_router,
)
from app.interfaces.endpoints.user_run_routes import router as user_run_router
from app.interfaces.http.admin.auth import router as admin_auth_router
from app.interfaces.http.admin.diagnostics import router as admin_diagnostics_router
from app.interfaces.http.internal.delivery_metrics import (
    router as delivery_metrics_router,
)
from app.interfaces.http.middleware.auth import require_knowbase_admin
from app.interfaces.http.web.auth import router as web_auth_router
from app.interfaces.http.web.cross_app import router as web_cross_app_router
from app.interfaces.http.web.interactions import router as web_interactions_router
from app.interfaces.mcp.data_mcp import build_router as build_data_mcp_router

router = APIRouter()
router.include_router(admin_auth_router)
router.include_router(web_auth_router)
router.include_router(admin_diagnostics_router)
router.include_router(delivery_metrics_router)
router.include_router(web_interactions_router)
router.include_router(web_cross_app_router)
router.include_router(
    knowledge_admin_router,
    dependencies=[Depends(require_knowbase_admin)],
)
router.include_router(knowledge_internal_router)
# 用户资料：数据第四版 user-data/v2（SDD 0043）
router.include_router(user_library_router)
router.include_router(user_run_router)
router.include_router(user_resolution_router)
router.include_router(user_fact_router)
# knowbase web端自家的资料库（SDD 0044）：本人会话，复用同一套服务
router.include_router(library_web_router)
# 管理端：用户资料的用量、词表、禁用资料的处理（0044 第四节）
router.include_router(user_data_admin_router)
# 数据 MCP 第二版（/api/mcp/data）
router.include_router(build_data_mcp_router())
