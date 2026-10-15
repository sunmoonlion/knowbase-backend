"""知识服务 MCP 服务端（F-KNOW-01/02/03/05）。

Streamable HTTP，只用 JSON 响应，不开 SSE 流。
- 鉴权：`Authorization: Bearer <token>`；令牌表来自配置（第一期静态，D10），
  每个令牌绑用户、沙箱与允许的工具清单；
- `tools/list` 按令牌过滤；`tools/call` 越权即拒绝并计数（异常调用上报的最小形态）；
- 限流：每令牌每分钟调用数封顶（进程内计数，第一期够用）；
- 多数据集（0008-info 段三）：三个查询工具可带 `dataset`，不带就是默认数据集；
  `list_datasets` 列出现有数据集。没打开多数据集时只有默认数据集。
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections import defaultdict
from dataclasses import dataclass
from typing import Any

from fastapi import APIRouter, Header, Request, Response
from fastapi.responses import JSONResponse
from joserfc import jwt
from joserfc.errors import JoseError
from joserfc.jwk import ECKey

from app.application.services.call_rate import RateLimiter
from app.application.services.dataset_catalog import (
    DatasetCatalog,
    DatasetQueries,
    UnknownDataset,
)
from app.application.services.dataset_query import SqlRejected
from app.bootstrap.datasets import build_datasets
from app.infrastructure.external.dataset_store import DatasetUnavailable
from core.config import Settings, get_settings

log = logging.getLogger(__name__)

PROTOCOL_VERSIONS = ("2025-06-18", "2025-03-26", "2024-11-05")
SERVER_KNOWFEED = {"name": "sunmoon-knowbase", "version": "0.2.0"}
_DATASET_ARG = {
    "type": "string",
    "description": (
        "Dataset to use, as returned by list_datasets. "
        "Omit only when that list marks one as default."
    ),
}
ALL_TOOLS: dict[str, dict[str, Any]] = {
    "list_datasets": {
        "description": (
            "List the datasets that can be queried: dataset, title, security_code, "
            "data_version, start_date, end_date. A security that is not listed has "
            "not been ingested; say so instead of guessing."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {},
            "additionalProperties": False,
        },
    },
    "describe_schema": {
        "description": (
            "List tables and columns of the dataset (optionally one table) with "
            "row counts. Every result carries a citation "
            "(dataset_id, data_version, as_of)."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {"table": {"type": "string"}, "dataset": _DATASET_ARG},
            "additionalProperties": False,
        },
    },
    "metric_definitions": {
        "description": (
            "口径表：metric name, display name, source table, expression hint, "
            "unit, time basis. Versioned with the dataset. `metric` may be the "
            "metric name or its display name (e.g. 净营收); exact match first, "
            "then substring. Omit it to list all."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "metric": {
                    "type": "string",
                    "description": (
                        "metric_name or display_name, e.g. net_revenue_cents or 净营收"
                    ),
                },
                "dataset": _DATASET_ARG,
            },
            "additionalProperties": False,
        },
    },
    "run_sql": {
        "description": (
            "Run ONE read-only SELECT against the dataset. At most 200 rows are "
            "returned; the result reports truncation and a citation with "
            "data_version. Cite data_version in any number you report."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "sql": {"type": "string"},
                "max_rows": {"type": "integer", "minimum": 1, "maximum": 200},
                "dataset": _DATASET_ARG,
            },
            "required": ["sql"],
            "additionalProperties": False,
        },
    },
}


SEMANTIC_TOOLS: dict[str, dict[str, Any]] = {
    "query_metric": {
        "description": (
            "Compute metrics BY NAME from their definitions in the dataset, one "
            "result row per row of the metric's base table (for financial "
            "statements: one company, one reporting period). Prefer this over "
            "writing the formula yourself. Each metric comes back with value, unit, "
            "applicable and reason: when applicable is false the metric must be "
            "reported as not applicable with that reason, never as a number. "
            "Call metric_definitions first: only metrics with queryable=true can be "
            "used, and all metrics in one call must share the same base_table. "
            "filters and order_by may use the key and label columns returned in "
            "each row."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "dataset": _DATASET_ARG,
                "metrics": {
                    "type": "array",
                    "items": {"type": "string"},
                    "minItems": 1,
                    "maxItems": 10,
                },
                "filters": {
                    "type": "array",
                    "maxItems": 10,
                    "items": {
                        "type": "object",
                        "properties": {
                            "column": {"type": "string"},
                            "op": {
                                "type": "string",
                                "enum": [
                                    "eq",
                                    "neq",
                                    "gt",
                                    "gte",
                                    "lt",
                                    "lte",
                                    "in",
                                    "not_in",
                                ],
                            },
                            "value": {},
                        },
                        "required": ["column", "op", "value"],
                        "additionalProperties": False,
                    },
                },
                "order_by": {
                    "type": "array",
                    "maxItems": 5,
                    "items": {
                        "type": "object",
                        "properties": {
                            "by": {"type": "string"},
                            "direction": {"type": "string", "enum": ["asc", "desc"]},
                        },
                        "required": ["by"],
                        "additionalProperties": False,
                    },
                },
                "max_rows": {"type": "integer", "minimum": 1, "maximum": 200},
            },
            "required": ["metrics"],
            "additionalProperties": False,
        },
    },
}
# 令牌里写的工具名按这份清单认；实例对外给哪些，看语义层开没开
KNOWN_TOOLS = frozenset(ALL_TOOLS) | frozenset(SEMANTIC_TOOLS)
SEMANTIC_SQL_NOTE = (
    " The SQL dialect is DuckDB. Use table names alone (no schema or database "
    "prefix). Dates are stored as text in ISO format. Dividing two integers "
    "yields a decimal."
)


def tool_specs(*, semantic: bool) -> dict[str, dict[str, Any]]:
    """这个实例对外的工具清单。语义层打开时，说明里写明方言。"""
    if not semantic:
        return ALL_TOOLS
    tools = {name: dict(spec) for name, spec in ALL_TOOLS.items()}
    tools["run_sql"]["description"] += SEMANTIC_SQL_NOTE
    tools.update(SEMANTIC_TOOLS)
    return tools


@dataclass(frozen=True)
class TokenGrant:
    user: str
    sandbox: str
    tools: frozenset[str]


class TokenTable:
    """令牌 → 授权。

    静态表 JSON：{"<token>": {"user": "...", "sandbox": "...", "tools": [..]|null}}；
    tools 为空即全部。
    D10：配了工作台公钥时，三段式令牌按 ES256 JWT 验签（aud=knowbase、exp、可选 iss），
    claims 里的 sub/sandbox/tools 就是授权；验不过不回退到静态表。
    """

    def __init__(
        self,
        raw: str,
        *,
        public_key_pem: str | None = None,
        issuer: str | None = None,
    ) -> None:
        self.grants: dict[str, TokenGrant] = {}
        for token, spec in (json.loads(raw or "{}") or {}).items():
            tools = spec.get("tools")
            self.grants[str(token)] = TokenGrant(
                user=str(spec.get("user", "")),
                sandbox=str(spec.get("sandbox", "")),
                tools=frozenset(tools) if tools else KNOWN_TOOLS,
            )
        self.public_key = ECKey.import_key(public_key_pem) if public_key_pem else None
        self.issuer = issuer

    def resolve(self, authorization: str | None) -> TokenGrant | None:
        scheme, _, token = (authorization or "").partition(" ")
        if scheme.lower() != "bearer" or not token or " " in token:
            return None
        if self.public_key is not None and token.count(".") == 2:
            return self._resolve_jwt(token)
        return self.grants.get(token)

    def _resolve_jwt(self, token: str) -> TokenGrant | None:
        assert self.public_key is not None
        try:
            claims = jwt.decode(token, self.public_key, algorithms=["ES256"]).claims
        except JoseError:
            return None
        if claims.get("aud") != "knowbase" or not claims.get("sub"):
            return None
        if self.issuer and claims.get("iss") != self.issuer:
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
        return TokenGrant(
            user=str(claims["sub"]),
            sandbox=str(claims.get("sandbox", "")),
            tools=frozenset(tools) & KNOWN_TOOLS if tools else KNOWN_TOOLS,
        )


class KnowledgeMcp:
    def __init__(
        self, settings: Settings, catalog: DatasetCatalog | None = None
    ) -> None:
        self.settings = settings
        self.tokens = TokenTable(
            settings.knowbase_mcp_tokens_json,
            public_key_pem=settings.knowbase_mcp_jwt_public_key,
            issuer=settings.knowbase_mcp_jwt_issuer,
        )
        self.limiter = RateLimiter(settings.knowbase_mcp_rate_per_minute)
        self.semantic = settings.knowbase_semantic_engine_enabled
        self.tools = tool_specs(semantic=self.semantic)
        self.anomalies: dict[str, int] = defaultdict(int)
        # 默认数据集与目录在装配层建（app/bootstrap/datasets.py）：页面用的也是那一份
        self.dataset: DatasetQueries | None = None
        if catalog is None:
            datasets = build_datasets(settings)
            catalog, self.dataset = datasets.catalog, datasets.default
        self.catalog = catalog

    # ---------------- JSON-RPC ----------------
    async def handle(
        self, grant: TokenGrant, message: dict[str, Any]
    ) -> dict[str, Any] | None:
        method = message.get("method")
        mid = message.get("id")
        params = message.get("params") or {}
        if method == "initialize":
            requested = str(params.get("protocolVersion") or PROTOCOL_VERSIONS[0])
            version = (
                requested if requested in PROTOCOL_VERSIONS else PROTOCOL_VERSIONS[0]
            )
            return _ok(
                mid,
                {
                    "protocolVersion": version,
                    "capabilities": {"tools": {"listChanged": False}},
                    "serverInfo": SERVER_KNOWFEED,
                    "instructions": (
                        "Read-only dataset tools. Cite data_version from each result."
                    ),
                },
            )
        if method in ("notifications/initialized", "notifications/cancelled"):
            return None
        if method == "ping":
            return _ok(mid, {})
        if method == "tools/list":
            tools = [
                {"name": name, **spec}
                for name, spec in self.tools.items()
                if name in grant.tools
            ]
            return _ok(mid, {"tools": tools})
        if method == "tools/call":
            return await self._call(grant, mid, params)
        if mid is None:
            return None
        return _err(mid, -32601, f"method not found: {method}")

    async def _call(
        self, grant: TokenGrant, mid: Any, params: dict[str, Any]
    ) -> dict[str, Any]:
        name = str(params.get("name") or "")
        args = params.get("arguments") or {}
        if name not in self.tools:
            return _err(mid, -32602, f"unknown tool: {name}")
        if name not in grant.tools:
            self.anomalies[grant.user] += 1
            log.warning(
                "knowledge_mcp_forbidden_tool user=%s sandbox=%s tool=%s",
                grant.user,
                grant.sandbox,
                name,
            )
            return _tool_error(mid, f"tool {name} is not allowed for this token")
        if not self.limiter.allow(f"{grant.user}/{grant.sandbox}"):
            self.anomalies[grant.user] += 1
            return _tool_error(mid, "rate limit exceeded; retry later")
        try:
            if name == "list_datasets":
                result: dict[str, Any] = {"datasets": await self.catalog.describe()}
            else:
                dataset = await self.catalog.resolve(args.get("dataset"))
                if name == "describe_schema":
                    result = await asyncio.to_thread(
                        dataset.describe_schema, args.get("table")
                    )
                elif name == "metric_definitions":
                    result = await asyncio.to_thread(
                        dataset.metric_definitions, args.get("metric")
                    )
                elif name == "query_metric":
                    # 只有语义层开着才有这个工具，此时数据集都是语义层的实现
                    result = await asyncio.to_thread(
                        dataset.query_metric,  # type: ignore[attr-defined]
                        args.get("metrics"),
                        filters=args.get("filters"),
                        order_by=args.get("order_by"),
                        max_rows=args.get("max_rows"),
                    )
                else:
                    result = await asyncio.to_thread(
                        dataset.run_sql,
                        str(args.get("sql") or ""),
                        max_rows=args.get("max_rows"),
                    )
        except (SqlRejected, UnknownDataset) as exc:
            return _tool_error(mid, str(exc))
        except (FileNotFoundError, DatasetUnavailable) as exc:
            log.error("knowledge_mcp_dataset_unavailable %s", exc)
            return _tool_error(mid, "dataset unavailable")
        except Exception:  # 登记表查不了等：不把内部错误细节给模型
            log.exception("knowledge_mcp_tool_failed tool=%s", name)
            return _tool_error(mid, "dataset service is temporarily unavailable")
        text = json.dumps(result, ensure_ascii=False, default=str)
        return _ok(
            mid,
            {
                "content": [{"type": "text", "text": text}],
                "structuredContent": result,
                "isError": False,
            },
        )


def _ok(mid: Any, result: dict[str, Any]) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": mid, "result": result}


def _err(mid: Any, code: int, message: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": mid, "error": {"code": code, "message": message}}


def _tool_error(mid: Any, message: str) -> dict[str, Any]:
    return _ok(mid, {"content": [{"type": "text", "text": message}], "isError": True})


def build_router(
    settings: Settings | None = None, catalog: DatasetCatalog | None = None
) -> APIRouter:
    server = KnowledgeMcp(settings or get_settings(), catalog)
    router = APIRouter(prefix="/mcp/knowledge", tags=["Knowledge MCP"])

    @router.post("")
    async def rpc(
        request: Request,
        authorization: str | None = Header(default=None, alias="Authorization"),
    ) -> Response:
        grant = server.tokens.resolve(authorization)
        if grant is None:
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
                reply = await server.handle(grant, m)
                if reply is not None:
                    replies.append(reply)
        if not replies:
            return Response(status_code=202)
        payload: Any = replies if isinstance(body, list) else replies[0]
        return JSONResponse(payload, headers={"Cache-Control": "no-store"})

    @router.get("")
    async def no_stream() -> Response:
        # 不提供服务端推送流；客户端应只用 POST
        return Response(status_code=405, headers={"Allow": "POST, DELETE"})

    @router.delete("")
    async def end_session() -> Response:
        return Response(status_code=204)

    router.state = server  # type: ignore[attr-defined]  # 测试与诊断用
    return router
