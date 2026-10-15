"""只挂知识 MCP 的最小 ASGI 应用：本机联调与评测用，不初始化 Postgres/Redis。

生产里同一路由也挂在主应用（routes.py），这里只是少一层依赖。
"""

from __future__ import annotations

from fastapi import FastAPI

from app.interfaces.errors.exception_handlers import register_exception_handlers
from app.interfaces.mcp.knowbase_mcp import build_router
from core.config import Settings, get_settings


def create_mcp_app(settings: Settings | None = None) -> FastAPI:
    app = FastAPI(
        title="Knowledge MCP", docs_url=None, redoc_url=None, openapi_url=None
    )
    register_exception_handlers(app)
    app.include_router(build_router(settings or get_settings()), prefix="/api")
    return app


app = create_mcp_app()
