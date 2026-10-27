"""数据 MCP（SDD 0048）：模型读钉住的输入、查事实、找证据、按描述查。

Streamable HTTP，只用 JSON 响应，不开流（同知识服务 MCP）。沙箱与用户 link 经 knowbench
中继到这里（knowbench 只转发、不验不换令牌）。

- 令牌：knowbench 签的 ES256，`aud` 必须是 `knowbase-data`（别的服务的令牌不认），
  `sub` 用户、`azp` 执行的应用、`sandbox` 沙箱或设备；令牌只证明身份。
- 每次调用另带 `run`（委托、测试编号）或 `session`（会话编号），knowbase 按它找这个
  用户、这个应用还有效的授权再核；授权撤了立即读不到，不看令牌。
- 会话里第一次读一份，knowbase 凭会话范围当场签一条小授权（0048 第五节）。
- 每个数、每段文字都带出处：资料、名字、内容版本、读法版本、页码。
"""

from __future__ import annotations

import json
import logging
import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Any

from fastapi import APIRouter, Header, Request, Response
from fastapi.responses import JSONResponse
from joserfc import jwt
from joserfc.errors import JoseError
from joserfc.jwk import ECKey

from app.application.services.call_rate import RateLimiter
from app.application.services.user_library_service import UserLibraryService
from app.application.services.user_resolution_service import UserResolutionService
from app.application.services.user_run_service import UserRunService
from app.domain.chart_of_accounts import METRICS
from app.domain.chart_of_accounts import snapshot as chart_snapshot
from app.domain.user_data import UserDataError
from app.domain.user_facts import FactQuery, FactRow, MetricRow, split_refs
from app.domain.user_library import PUBLIC_OWNER, DescriptionFilter
from app.domain.user_resolution import DescriptionKey
from app.domain.user_runs import RunGrant
from app.domain.vocabulary import snapshot as vocabulary_snapshot
from core.config import Settings, get_settings

log = logging.getLogger(__name__)

AUDIENCE = "knowbase-data"
EXECUTORS = ("knowbench", "knowlab")
PROTOCOL_VERSIONS = ("2025-06-18", "2025-03-26")
MAX_TEXT_CHARS = 60_000
MAX_PAGES = 30
SNIPPET_CHARS = 300

_RUN = {"type": "string", "description": "委托或测试编号（说明里给的）。"}
_SESSION = {"type": "string", "description": "会话编号（会话里用这个，不给 run）。"}
_FILTERS = {
    "subjects": {"type": "array", "items": {"type": "string"}},
    "codes": {
        "type": "array",
        "items": {"type": "string"},
        "description": "科目编码，见 describe_items。",
    },
    "period_from": {"type": "string", "format": "date"},
    "period_to": {"type": "string", "format": "date"},
    "duration": {"enum": ["year", "half", "quarter", "ytd", "instant"]},
    "consolidation": {"enum": ["consolidated", "parent"]},
}

# 按描述的维度圈（SDD 0049）：取值见 describe_items 里的词表
_DIMENSIONS = {
    "doc_type": {"type": "string", "description": "A type or a class."},
    "subjects": {"type": "array", "items": {"type": "string"}},
    "period_from": {"type": "string", "format": "date"},
    "period_to": {"type": "string", "format": "date"},
    "disclosed_from": {"type": "string", "format": "date"},
    "disclosed_to": {"type": "string", "format": "date"},
    "edition": {"enum": ["full", "summary", "english"]},
    "topics": {"type": "array", "items": {"type": "string"}},
    "industry": {"type": "string"},
    "language": {"enum": ["zh", "en", "other"]},
    "research": {
        "type": "object",
        "description": "forecast, market, depth or frequency values.",
    },
}

TOOLS: dict[str, dict[str, Any]] = {
    "list_inputs": {
        "description": (
            "List what this run may read: each material, its description, the pinned "
            "version, which readings (text, pages, table) are ready, and whose it is."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {"run": _RUN},
            "required": ["run"],
        },
    },
    "read_input": {
        "description": (
            "Read one input of this run (or, in a session, any material the user "
            "shows to this app or a public original). what=text|pages|format|original;"
            " pages takes page_from/page_to. Cite the source returned."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "run": _RUN,
                "session": _SESSION,
                "material_id": {"type": "string"},
                "version_no": {"type": "integer"},
                "what": {"enum": ["text", "pages", "format", "original"]},
                "page_from": {"type": "integer"},
                "page_to": {"type": "integer"},
                "offset": {"type": "integer"},
            },
            "required": ["material_id", "what"],
        },
    },
    "query_facts": {
        "description": (
            "Query financial statement facts. scope=run: only the run's pinned "
            "materials (in a session: the ones already read); scope=market: public "
            "originals across the whole market (screen with value_min/value_max, "
            "rank with order). Every row has a ref (use it as the address of the "
            "number, and as an input of compute) and cites material, version, "
            "reading and page."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "run": _RUN,
                "session": _SESSION,
                "scope": {"enum": ["run", "market"]},
                "layer": {"enum": ["standard", "raw"]},
                **_FILTERS,
                "labels": {"type": "array", "items": {"type": "string"}},
                "value_min": {"type": "number"},
                "value_max": {"type": "number"},
                "order": {"enum": ["period", "value_desc", "value_asc"]},
                "limit": {"type": "integer"},
            },
        },
    },
    "query_metric": {
        "description": (
            "Compute a metric (see describe_items) from standardized facts, with the "
            "formula's inputs and their sources. scope=run or market."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "metric": {"type": "string"},
                "run": _RUN,
                "session": _SESSION,
                "scope": {"enum": ["run", "market"]},
                **_FILTERS,
                "limit": {"type": "integer"},
            },
            "required": ["metric"],
        },
    },
    "read_facts": {
        "description": (
            "Read facts by their refs (fact:<id> or raw:<id>, as returned by "
            "query_facts). scope=run: only the run's pinned materials (in a session: "
            "the ones already read); scope=market: public originals. Refs that are "
            "not readable here come back in not_found."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "run": _RUN,
                "session": _SESSION,
                "scope": {"enum": ["run", "market"]},
                "refs": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["refs"],
        },
    },
    "describe_items": {
        "description": "The chart of accounts (codes, names, statements, company "
        "types), the metric definitions, and the vocabulary of material "
        "descriptions (types and classes, topics, industries, editions).",
        "inputSchema": {"type": "object", "properties": {}},
    },
    "search_evidence": {
        "description": (
            "Find passages in this run's pinned materials (or, in a session, the ones "
            "already read). Narrow the materials first by their description: doc_type "
            "(a type such as annual_report, or a class such as disclosure or "
            "research), subjects, report period, disclosure dates, edition, topics, "
            "industry, language, research dimensions. Returns page snippets with "
            "sources."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "run": _RUN,
                "session": _SESSION,
                "query": {"type": "string"},
                "top_k": {"type": "integer"},
                **_DIMENSIONS,
            },
            "required": ["query"],
        },
    },
    "lookup": {
        "description": (
            "Is this exact document there (subject, doc_type, report period, "
            "edition full/summary/english)? doc_type is a type from the vocabulary, "
            "e.g. annual_report. Answers shown/held/unusable/absent and where (own or "
            "public)."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "subject": {"type": "string"},
                "doc_type": {"type": "string"},
                "period_start": {"type": "string", "format": "date"},
                "period_end": {"type": "string", "format": "date"},
                "edition": {"enum": ["full", "summary", "english"]},
            },
            "required": ["subject", "doc_type"],
        },
    },
    "list_library": {
        "description": (
            "In a session: the materials the user shows to this app and his "
            "references to public originals. Public originals can also be found "
            "with lookup and read directly."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {"session": _SESSION},
            "required": ["session"],
        },
    },
}


@dataclass(frozen=True)
class DataToken:
    user: str
    executor: str
    sandbox: str
    tools: frozenset[str]


class ToolError(Exception):
    """给模型看的错误（不含内部细节）。"""


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
    allowed = frozenset(tools) & frozenset(TOOLS) if tools else frozenset(TOOLS)
    return DataToken(
        user=str(claims["sub"]),
        executor=str(claims["azp"]),
        sandbox=str(claims["sandbox"]),
        tools=allowed,
    )


class DataMcp:
    def __init__(
        self,
        runs: Callable[[], UserRunService],
        resolver: Callable[[], UserResolutionService],
        library: Callable[[], UserLibraryService],
        *,
        rate_per_minute: int = 120,
    ) -> None:
        self.runs = runs
        self.resolver = resolver
        self.library = library
        self.limiter = RateLimiter(rate_per_minute)

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
                        for name, spec in TOOLS.items()
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
        if name not in TOOLS:
            return _err(mid, -32602, f"unknown tool: {name}")
        if name not in token.tools:
            log.warning(
                "data_mcp_forbidden_tool user=%s sandbox=%s tool=%s",
                token.user,
                token.sandbox,
                name,
            )
            return _tool_error(mid, f"tool {name} is not allowed for this token")
        if not self.limiter.allow(f"{token.user}/{token.sandbox}"):
            return _tool_error(mid, "rate limit exceeded; retry later")
        try:
            result = await getattr(self, f"_{name}")(token, args)
        except ToolError as exc:
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

    # —— 工具 ——

    async def _list_inputs(self, token: DataToken, args: dict[str, Any]) -> Any:
        run = _text(args, "run")
        runs = self.runs()
        grants = await runs.run_grants(token.executor, token.user, run)
        inputs = []
        for grant in grants:
            for item in grant.items:
                inputs.append(
                    await self._describe(runs, grant, item.material_id, item.version_no)
                )
        return {"run": run, "inputs": inputs}

    async def _read_input(self, token: DataToken, args: dict[str, Any]) -> Any:
        runs = self.runs()
        material_id = _text(args, "material_id")
        version_no = _int(args, "version_no")
        what = _text(args, "what")
        grant = await self._grant_for(runs, token, args, material_id)
        reader = f"mcp:{token.sandbox}"
        if what in ("text", "pages"):
            got, rendition = await runs.read_reading(
                token.executor, reader, grant.id, material_id, version_no, what
            )
            source = _source(got.item.material_id, got.version, rendition)
            text = (got.content or b"").decode("utf-8", "replace")
            if what == "pages":
                pages = json.loads(text)
                first = _int(args, "page_from") or 1
                last = min(
                    _int(args, "page_to") or first + MAX_PAGES - 1,
                    first + MAX_PAGES - 1,
                )
                chosen = [p for p in pages if first <= int(p["page"]) <= last]
                return {"source": source, "pages": chosen, "total_pages": len(pages)}
            offset = max(_int(args, "offset") or 0, 0)
            part = text[offset : offset + MAX_TEXT_CHARS]
            return {
                "source": source,
                "text": part,
                "offset": offset,
                "more": offset + len(part) < len(text),
            }
        if what == "format":
            got = await runs.read(
                token.executor, reader, grant.id, material_id, version_no, "format"
            )
            return {
                "source": _source(got.item.material_id, got.version, None),
                "media_type": got.version.media_type,
                "sheets": list(got.format.sheets) if got.format else [],
                "header": got.format.header if got.format else None,
            }
        if what == "original":
            got = await runs.read(
                token.executor, reader, grant.id, material_id, version_no, "content"
            )
            if not got.version.media_type.startswith("text/"):
                raise ToolError(
                    "this original is not text; read its text or pages reading"
                )
            text = (got.content or b"").decode("utf-8", "replace")
            return {
                "source": _source(got.item.material_id, got.version, None),
                "text": text[:MAX_TEXT_CHARS],
                "more": len(text) > MAX_TEXT_CHARS,
            }
        raise ToolError("what is text, pages, format or original")

    async def _query_facts(self, token: DataToken, args: dict[str, Any]) -> Any:
        runs = self.runs()
        query = _query(args)
        if args.get("scope") == "market":
            rows = await runs.query_market(query)
        else:
            rows = await runs.query_facts(
                token.executor,
                f"mcp:{token.sandbox}",
                token.user,
                await _run_or_session(runs, token, args),
                query,
            )
        return {"facts": [_fact(r) for r in rows]}

    async def _read_facts(self, token: DataToken, args: dict[str, Any]) -> Any:
        runs = self.runs()
        refs = args.get("refs")
        if not isinstance(refs, list):
            raise ValueError("refs must be a list")
        wanted = [str(r) for r in refs]
        market = args.get("scope") == "market"
        ref = None if market else await _run_or_session(runs, token, args)
        rows: list[FactRow] = []
        for layer, ids in split_refs(wanted).items():
            query = FactQuery(
                layer=layer,  # type: ignore[arg-type]
                ids=ids,
                consolidation=None,
                limit=len(ids),
            )
            if ref is None:
                rows += await runs.query_market(query)
            else:
                rows += await runs.query_facts(
                    token.executor, f"mcp:{token.sandbox}", token.user, ref, query
                )
        found = {r.ref: r for r in rows}
        return {
            "facts": [_fact(found[r]) for r in dict.fromkeys(wanted) if r in found],
            "not_found": [r for r in dict.fromkeys(wanted) if r not in found],
        }

    async def _query_metric(self, token: DataToken, args: dict[str, Any]) -> Any:
        runs = self.runs()
        metric = _text(args, "metric")
        query = _query(args)
        if args.get("scope") == "market":
            rows = await runs.query_metric(metric, query)
        else:
            rows = await runs.query_metric(
                metric,
                query,
                executor_app=token.executor,
                reader=f"mcp:{token.sandbox}",
                beneficiary=token.user,
                run_ref=await _run_or_session(runs, token, args),
            )
        definition = METRICS[metric]
        return {
            "metric": metric,
            "name": definition.name,
            "formula": definition.description,
            "values": [_metric(m) for m in rows],
        }

    async def _describe_items(self, token: DataToken, args: dict[str, Any]) -> Any:
        return {
            **chart_snapshot(),
            "vocabulary": vocabulary_snapshot(),
            "metrics": [
                {"key": m.key, "name": m.name, "kind": m.kind, "formula": m.description}
                for m in METRICS.values()
            ],
        }

    async def _search_evidence(self, token: DataToken, args: dict[str, Any]) -> Any:
        """在钉住的版本（会话里是读过的）的按页文字里按关键词找，回页级片段与出处。
        块 9（RAGFlow 一版本一文档）上线后换成关键词加向量的混合检索。"""
        runs = self.runs()
        terms = [t for t in re.split(r"\s+", _text(args, "query")) if t]
        if not terms:
            raise ToolError("query is empty")
        top_k = min(max(_int(args, "top_k") or 5, 1), 20)
        narrow = _dimension_filter(args)
        if args.get("session"):
            grants = await runs.session_grants(
                token.user, token.executor, _text(args, "session")
            )
        else:
            grants = await runs.run_grants(
                token.executor, token.user, _text(args, "run")
            )
        hits: list[tuple[int, dict[str, Any]]] = []
        reader = f"mcp:{token.sandbox}"
        searched = 0
        for grant in grants:
            for item in grant.items:
                if not narrow.empty:
                    material = await runs.library.get_material(item.material_id)
                    if material is None or not narrow.matches(material.description):
                        continue
                searched += 1
                try:
                    got, rendition = await runs.read_reading(
                        token.executor,
                        reader,
                        grant.id,
                        item.material_id,
                        item.version_no,
                        "pages",
                    )
                except UserDataError:
                    continue
                source = _source(item.material_id, got.version, rendition)
                for page in json.loads((got.content or b"[]").decode("utf-8")):
                    body = str(page.get("text") or "")
                    score = sum(body.count(t) for t in terms)
                    if score:
                        at = min(body.find(t) for t in terms if t in body)
                        start = max(at - SNIPPET_CHARS // 3, 0)
                        hits.append(
                            (
                                score,
                                {
                                    "source": {**source, "page": page.get("page")},
                                    "snippet": body[start : start + SNIPPET_CHARS],
                                    "score": score,
                                },
                            )
                        )
        hits.sort(key=lambda h: -h[0])
        return {
            "method": "keyword",
            "materials_searched": searched,
            "passages": [h for _, h in hits[:top_k]],
        }

    async def _lookup(self, token: DataToken, args: dict[str, Any]) -> Any:
        key = DescriptionKey(
            _text(args, "subject"),
            _text(args, "doc_type"),
            _date(args, "period_start"),
            _date(args, "period_end"),
            edition=str(args.get("edition") or "full"),
        )
        status, zone = await self.resolver().lookup(token.user, token.executor, key)
        return {"status": status, "zone": zone}

    async def _list_library(self, token: DataToken, args: dict[str, Any]) -> Any:
        runs, library = self.runs(), self.library()
        await runs.session_grants(token.user, token.executor, _text(args, "session"))
        shown = await library.column(token.user, token.executor)
        references = [
            e for e in await library.shelf(token.user) if e.holding.basis == "reference"
        ]
        out = []
        for entry in [*shown, *references]:
            d = entry.material.description
            out.append(
                {
                    "material_id": entry.material.id,
                    "name": entry.material.name,
                    "version_no": entry.version.version_no,
                    "subject": d.subject,
                    "doc_type": d.doc_type,
                    "period_end": d.period_end,
                    "category": entry.category,
                }
            )
        return {"materials": out}

    # —— 内部 ——

    async def _grant_for(
        self,
        runs: UserRunService,
        token: DataToken,
        args: dict[str, Any],
        material_id: str,
    ) -> RunGrant:
        if args.get("session"):
            grant, total, hint = await runs.session_read(
                token.user, token.executor, _text(args, "session"), material_id
            )
            if hint:
                log.info("data_mcp_session_hint user=%s total=%s", token.user, total)
            return grant
        for grant in await runs.run_grants(
            token.executor, token.user, _text(args, "run")
        ):
            if any(i.material_id == material_id for i in grant.items):
                return grant
        raise ToolError("this material is not an input of this run")

    async def _describe(
        self, runs: UserRunService, grant: RunGrant, material_id: str, version_no: int
    ) -> dict[str, Any]:
        material = await runs.library.get_material(material_id)
        item = next(i for i in grant.items if i.material_id == material_id)
        renditions = await runs.renditions_of(material_id, version_no)
        d = material.description if material else None
        owner = (
            "public"
            if item.content_owner == PUBLIC_OWNER
            else ("yours" if item.content_owner == grant.beneficiary else "author")
        )
        return {
            "material_id": material_id,
            "name": material.name if material else None,
            "version_no": version_no,
            "subject": d.subject if d else None,
            "doc_type": d.doc_type if d else None,
            "period_start": d.period_start if d else None,
            "period_end": d.period_end if d else None,
            "readings": sorted({r.kind for r in renditions if r.status == "ready"}),
            "owner": owner,
            "basis": item.basis,
            "grant_revision": grant.revision,
        }


def _source(material_id: str, version, rendition) -> dict[str, Any]:
    return {
        "material_id": material_id,
        "version_no": version.version_no,
        "sha256": version.sha256[:8],
        "reading": f"{rendition.kind}/{rendition.rendition_no}" if rendition else None,
    }


async def _run_or_session(
    runs: UserRunService, token: DataToken, args: dict[str, Any]
) -> str:
    """查事实按哪件事的授权：委托、测试给 run；会话给 session（先核是他这段会话，
    会话的小授权以会话编号为 ref）。"""
    if args.get("session"):
        session_id = _text(args, "session")
        await runs.session_grants(token.user, token.executor, session_id)
        return session_id
    return _text(args, "run")


def _fact(r: FactRow) -> dict[str, Any]:
    return {
        "ref": r.ref,
        "subject": r.subject,
        "code": r.code,
        "label": r.label,
        "value": r.value,
        "unit": r.unit,
        "currency": r.currency,
        "period_end": r.period_end,
        "duration": r.duration,
        "consolidation": r.consolidation,
        "source": {
            "material_id": r.material_id,
            "name": r.material_name,
            "version_no": r.version_no,
            "reading": f"table/{r.rendition_no}",
            "page": r.page,
        },
    }


def _metric(m: MetricRow) -> dict[str, Any]:
    return {
        "subject": m.subject,
        "period_end": m.period_end,
        "duration": m.duration,
        "consolidation": m.consolidation,
        "value": m.value,
        "inputs": [_fact(i) for i in m.inputs],
    }


def _dimension_filter(args: dict[str, Any]) -> DescriptionFilter:
    def strings(key: str) -> tuple[str, ...]:
        value = args.get(key) or []
        if not isinstance(value, list):
            raise ValueError(f"{key} must be a list")
        return tuple(str(v) for v in value)

    research = args.get("research") or {}
    if not isinstance(research, dict):
        raise ValueError("research must be an object")
    return DescriptionFilter(
        doc_type=str(args["doc_type"]) if args.get("doc_type") else None,
        subjects=strings("subjects"),
        period_from=_date(args, "period_from"),
        period_to=_date(args, "period_to"),
        disclosed_from=_date(args, "disclosed_from"),
        disclosed_to=_date(args, "disclosed_to"),
        edition=str(args["edition"]) if args.get("edition") else None,
        topics=strings("topics"),
        industry=str(args["industry"]) if args.get("industry") else None,
        language=str(args["language"]) if args.get("language") else None,
        research={str(k): str(v) for k, v in research.items()},
    ).checked()


def _query(args: dict[str, Any]) -> FactQuery:
    def strings(key: str) -> tuple[str, ...]:
        value = args.get(key) or []
        if not isinstance(value, list):
            raise ValueError(f"{key} must be a list")
        return tuple(str(v) for v in value)

    def number(key: str) -> Decimal | None:
        value = args.get(key)
        return None if value is None else Decimal(str(value))

    return FactQuery(
        layer=args.get("layer") or "standard",
        subjects=strings("subjects"),
        codes=strings("codes"),
        labels=strings("labels"),
        period_from=_date(args, "period_from"),
        period_to=_date(args, "period_to"),
        duration=args.get("duration"),
        consolidation=args.get("consolidation", "consolidated"),
        value_min=number("value_min"),
        value_max=number("value_max"),
        order=args.get("order") or "period",
        limit=int(args.get("limit") or 200),
    )


def _text(args: dict[str, Any], key: str) -> str:
    value = args.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ToolError(f"{key} is required")
    return value.strip()


def _int(args: dict[str, Any], key: str) -> int | None:
    value = args.get(key)
    return None if value is None else int(value)


def _date(args: dict[str, Any], key: str) -> date | None:
    value = args.get(key)
    return None if value in (None, "") else date.fromisoformat(str(value))


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
        from app.interfaces.endpoints.user_library_routes import (
            get_user_library_service,
        )
        from app.interfaces.endpoints.user_resolution_routes import (
            get_user_resolution_service,
        )
        from app.interfaces.endpoints.user_run_routes import get_user_run_service

        server = DataMcp(
            lambda: get_user_run_service(s),
            get_user_resolution_service,
            lambda: get_user_library_service(s),
            rate_per_minute=s.knowbase_mcp_rate_per_minute,
        )
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
