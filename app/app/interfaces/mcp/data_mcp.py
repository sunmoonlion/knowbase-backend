"""Codex-facing protocol adapter for the KnowBase data functions.

The bearer identifies the user, executor, sandbox and allowed operations. Each
function checks its resource grants in KnowBase at execution time.
"""

from __future__ import annotations

import json
import logging
import time
from datetime import date
from decimal import Decimal
from typing import Any

from fastapi import APIRouter, Header, Request, Response
from fastapi.responses import JSONResponse
from joserfc import jwt
from joserfc.errors import JoseError
from joserfc.jwk import ECKey

from app.application.services.data_functions import (
    TOOLS,
    DataFunctionError,
    DataFunctions,
    DataToken,
)
from app.application.services.data_query_revisions import REGISTERED_QUERIES
from app.domain.method_reads import MethodRead
from app.domain.user_data import UserDataError
from core.config import Settings, get_settings

log = logging.getLogger(__name__)

MCP_TOOLS = {
    name: spec for name, spec in TOOLS.items() if name not in REGISTERED_QUERIES
}

AUDIENCE = "knowbase-data"
EXECUTORS = ("knowbench", "knowlab")
PROTOCOL_VERSIONS = ("2025-06-18", "2025-03-26")


def verify(
    authorization: str | None, public_key: ECKey | None, issuer: str | None
) -> DataToken | None:
    scheme, _, token = (authorization or "").partition(" ")
    if scheme.lower() != "bearer" or not token or public_key is None:
        return None
    try:
        claims = jwt.decode(token, public_key, algorithms=["ES256"]).claims
    except JoseError:
        return None
    if claims.get("aud") != AUDIENCE or not claims.get("sub"):
        return None
    if issuer and claims.get("iss") != issuer:
        return None
    if claims.get("azp") not in EXECUTORS or not claims.get("sandbox"):
        return None
    try:
        if int(claims.get("exp", 0)) <= time.time():
            return None
    except (TypeError, ValueError):
        return None
    tools = claims.get("tools")
    if tools is not None and not (
        isinstance(tools, list) and all(isinstance(t, str) for t in tools)
    ):
        return None
    allowed = frozenset(tools or ()) & frozenset(MCP_TOOLS)
    if not tools or "read_method_input" not in tools or claims["azp"] != "knowbench":
        allowed = allowed - {"read_method_input"}
    method_read = None
    if "method_read" in claims:
        if allowed != {"read_method_input"} or tools != ["read_method_input"]:
            return None
        try:
            method_read = MethodRead.model_validate(claims["method_read"])
        except ValueError:
            return None
    return DataToken(
        user=str(claims["sub"]),
        executor=str(claims["azp"]),
        sandbox=str(claims["sandbox"]),
        tools=allowed,
        method_read=method_read,
    )


class DataMcp:
    def __init__(self, functions: DataFunctions):
        self.functions = functions

    async def handle(self, token: DataToken, message: dict[str, Any]) -> Any:
        method = message.get("method")
        mid = message.get("id")
        params = message.get("params") or {}
        if method == "initialize":
            requested = str(params.get("protocolVersion") or PROTOCOL_VERSIONS[0])
            return _ok(
                mid,
                {
                    "protocolVersion": requested
                    if requested in PROTOCOL_VERSIONS
                    else PROTOCOL_VERSIONS[0],
                    "capabilities": {"tools": {"listChanged": False}},
                    "serverInfo": {"name": "knowbase-data", "version": "1"},
                    "instructions": (
                        "Read-only data tools. Pass run (or session) as given in your "
                        "instructions. Cite the source of every figure and passage. "
                        "If data is missing, ask with request_data; never assume it."
                    ),
                },
            )
        if method in ("notifications/initialized", "notifications/cancelled"):
            return None
        if method == "ping":
            return _ok(mid, {})
        if method == "tools/list":
            return _ok(
                mid,
                {
                    "tools": [
                        {"name": name, **spec}
                        for name, spec in MCP_TOOLS.items()
                        if name in token.tools
                    ]
                },
            )
        if method == "tools/call":
            return await self._call(token, mid, params)
        if mid is None:
            return None
        return _err(mid, -32601, f"method not found: {method}")

    async def _call(self, token: DataToken, mid: Any, params: dict[str, Any]) -> Any:
        name = str(params.get("name") or "")
        args = params.get("arguments") or {}
        if name not in MCP_TOOLS:
            return _err(mid, -32602, f"unknown tool: {name}")
        try:
            result = await self.functions.invoke(token, name, args)
        except DataFunctionError as exc:
            return _tool_error(mid, str(exc))
        except UserDataError as exc:
            return _tool_error(mid, f"{exc.code}: {exc.message}")
        except (ValueError, TypeError) as exc:
            return _tool_error(mid, f"invalid arguments: {exc}")
        except Exception:
            log.exception("data_mcp_tool_failed tool=%s", name)
            return _tool_error(mid, "data service is temporarily unavailable")
        text = json.dumps(result, ensure_ascii=False, default=_json)
        return _ok(
            mid,
            {
                "content": [{"type": "text", "text": text}],
                "structuredContent": json.loads(text),
                "isError": False,
            },
        )


def _json(value: Any) -> Any:
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, date):
        return value.isoformat()
    raise TypeError(type(value).__name__)


def _ok(mid: Any, result: dict[str, Any]) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": mid, "result": result}


def _err(mid: Any, code: int, message: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": mid, "error": {"code": code, "message": message}}


def _tool_error(mid: Any, message: str) -> dict[str, Any]:
    return _ok(mid, {"content": [{"type": "text", "text": message}], "isError": True})


def build_data_functions(s: Settings):
    from app.interfaces.endpoints.user_library_routes import (
        get_user_library_service,
    )
    from app.interfaces.endpoints.user_resolution_routes import (
        get_user_resolution_service,
    )
    from app.interfaces.endpoints.user_run_routes import get_user_run_service

    return DataFunctions(
        lambda: get_user_run_service(s),
        get_user_resolution_service,
        lambda: get_user_library_service(s),
        rate_per_minute=s.knowbase_mcp_rate_per_minute,
    )


def build_router(
    settings: Settings | None = None, server: DataMcp | None = None
) -> APIRouter:
    s = settings or get_settings()
    public_key = (
        ECKey.import_key(s.knowbase_mcp_jwt_public_key)
        if s.knowbase_mcp_jwt_public_key
        else None
    )
    if server is None:
        server = DataMcp(build_data_functions(s))
    router = APIRouter(prefix="/mcp/data", tags=["Data MCP"])

    @router.post("")
    async def rpc(
        request: Request,
        authorization: str | None = Header(default=None, alias="Authorization"),
    ) -> Response:
        token = verify(authorization, public_key, s.knowbase_mcp_jwt_issuer)
        if token is None:
            return JSONResponse(
                {
                    "jsonrpc": "2.0",
                    "id": None,
                    "error": {"code": -32001, "message": "unauthorized"},
                },
                status_code=401,
                headers={"WWW-Authenticate": "Bearer"},
            )
        try:
            body = json.loads(await request.body())
        except ValueError:
            return JSONResponse(_err(None, -32700, "parse error"), status_code=400)
        messages = body if isinstance(body, list) else [body]
        replies = []
        for m in messages:
            if isinstance(m, dict):
                reply = await server.handle(token, m)
                if reply is not None:
                    replies.append(reply)
        if not replies:
            return Response(status_code=202)
        payload: Any = replies if isinstance(body, list) else replies[0]
        return JSONResponse(payload, headers={"Cache-Control": "no-store"})

    @router.get("")
    async def no_stream() -> Response:
        return Response(status_code=405, headers={"Allow": "POST, DELETE"})

    @router.delete("")
    async def end_session() -> Response:
        return Response(status_code=204)

    router.state = server  # type: ignore[attr-defined]
    return router
