"""KnowBase data functions enforce operation and resource grants on every call."""

from __future__ import annotations

import logging
import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Any

from app.application.services.call_rate import RateLimiter
from app.application.services.user_library_service import UserLibraryService
from app.application.services.user_resolution_service import UserResolutionService
from app.application.services.user_run_service import UserRunService
from app.domain.chart_of_accounts import METRICS
from app.domain.chart_of_accounts import snapshot as chart_snapshot
from app.domain.method_reads import MethodRead
from app.domain.user_facts import (
    MAX_FACT_REFS,
    FactQuery,
    FactRow,
    MetricRow,
    split_refs,
)
from app.domain.user_library import PUBLIC_OWNER, DescriptionFilter
from app.domain.user_resolution import DescriptionKey
from app.domain.user_runs import RunGrant
from app.domain.vocabulary import snapshot as vocabulary_snapshot

log = logging.getLogger(__name__)

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
    "read_method_input": {
        "description": "Read the pinned UTF-8 text approved for this invocation.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "material_id": {"type": "string", "format": "uuid"},
                "version_no": {"type": "integer", "minimum": 1},
            },
            "required": ["material_id", "version_no"],
            "additionalProperties": False,
        },
    },
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
                "refs": {
                    "type": "array",
                    "minItems": 1,
                    "maxItems": MAX_FACT_REFS,
                    "items": {"type": "string"},
                },
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
    method_read: MethodRead | None = None
    function_call_id: str | None = None

    @property
    def reader(self) -> str:
        if self.function_call_id:
            return f"function:{self.function_call_id}"
        return f"mcp:{self.sandbox}"


class DataFunctionError(Exception):
    """给模型看的错误（不含内部细节）。"""


class DataFunctions:
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

    async def invoke(
        self, token: DataToken, operation: str, arguments: dict[str, Any]
    ) -> Any:
        if operation not in TOOLS or operation not in token.tools:
            raise DataFunctionError(
                f"Operation {operation} is not allowed for this token"
            )
        if not self.limiter.allow(f"{token.user}/{token.sandbox}"):
            raise DataFunctionError("rate limit exceeded; retry later")
        return await getattr(self, f"_{operation}")(token, arguments)

    # —— 工具 ——

    async def _read_method_input(self, token: DataToken, args: dict[str, Any]) -> Any:
        read = token.method_read
        if (
            token.executor != "knowbench"
            or read is None
            or set(args) != {"material_id", "version_no"}
            or args["material_id"] != str(read.material_id)
            or type(args["version_no"]) is not int
            or args["version_no"] != read.version_no
        ):
            raise DataFunctionError("method read differs from its signed invocation")
        got, grant_id = await self.runs().read_for_method(token.user, read)
        try:
            content = (got.content or b"").decode("utf-8")
        except UnicodeDecodeError as exc:
            raise DataFunctionError(
                "method_input_encoding: pinned material requires UTF-8 text"
            ) from exc
        return {
            "source": {
                "material_id": got.item.material_id,
                "version_no": got.version.version_no,
                "sha256": got.version.sha256,
                "grant_id": grant_id,
                "method_call_id": str(read.path[-1].call_id),
            },
            "text": content,
        }

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
        reader = token.reader
        if what == "pages":
            first = max(_int(args, "page_from") or 1, 1)
            last = min(
                _int(args, "page_to") or first + MAX_PAGES - 1,
                first + MAX_PAGES - 1,
            )
            got = await runs.read_reading(
                token.executor,
                reader,
                grant.id,
                material_id,
                version_no,
                "pages",
                page_from=first,
                page_to=last,
            )
            return {
                "source": _source(got.item.material_id, got.version, got.rendition),
                "pages": [{"page": p.number, "text": p.text} for p in got.pages],
                "total_pages": got.total_pages,
            }
        if what == "text":
            got = await runs.read_reading(
                token.executor, reader, grant.id, material_id, version_no, "text"
            )
            assert got.text is not None
            text = got.text.body
            offset = max(_int(args, "offset") or 0, 0)
            part = text[offset : offset + MAX_TEXT_CHARS]
            return {
                "source": _source(got.item.material_id, got.version, got.rendition),
                "title": got.text.title,
                "published_on": got.text.published_on,
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
                raise DataFunctionError(
                    "this original is not text; read its text or pages reading"
                )
            text = (got.content or b"").decode("utf-8", "replace")
            return {
                "source": _source(got.item.material_id, got.version, None),
                "text": text[:MAX_TEXT_CHARS],
                "more": len(text) > MAX_TEXT_CHARS,
            }
        raise DataFunctionError("what is text, pages, format or original")

    async def _query_facts(self, token: DataToken, args: dict[str, Any]) -> Any:
        runs = self.runs()
        query = _query(args)
        if args.get("scope") == "market":
            rows = await runs.query_market(token.user, query)
        else:
            rows = await runs.query_facts(
                token.executor,
                token.reader,
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
                rows += await runs.query_market(token.user, query)
            else:
                rows += await runs.query_facts(
                    token.executor, token.reader, token.user, ref, query
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
            rows = await runs.query_metric(metric, query, subject=token.user)
        else:
            rows = await runs.query_metric(
                metric,
                query,
                executor_app=token.executor,
                reader=token.reader,
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
        按页文字在 knowbase 的 PostgreSQL 里，由库先挑出含关键词的页再打分。
        块 9（RAGFlow 一版本一文档）上线后换成关键词加向量的混合检索。"""
        runs = self.runs()
        terms = [t for t in re.split(r"\s+", _text(args, "query")) if t]
        if not terms:
            raise DataFunctionError("query is empty")
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
        targets = []
        for grant in grants:
            for item in grant.items:
                if not narrow.empty:
                    material = await runs.library.get_material(item.material_id)
                    if material is None or not narrow.matches(material.description):
                        continue
                targets.append((grant, item))
        found, searched = await runs.search_pages(
            token.executor, token.reader, targets, terms
        )
        hits: list[tuple[int, dict[str, Any]]] = []
        for reading, page in found:
            body = page.text
            score = sum(body.count(t) for t in terms)
            if not score:
                continue
            at = min(body.find(t) for t in terms if t in body)
            start = max(at - SNIPPET_CHARS // 3, 0)
            source = _source(
                reading.item.material_id, reading.version, reading.rendition
            )
            hits.append(
                (
                    score,
                    {
                        "source": {**source, "page": page.number},
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
        raise DataFunctionError("this material is not an input of this run")

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
        raise DataFunctionError(f"{key} is required")
    return value.strip()


def _int(args: dict[str, Any], key: str) -> int | None:
    value = args.get(key)
    return None if value is None else int(value)


def _date(args: dict[str, Any], key: str) -> date | None:
    value = args.get(key)
    return None if value in (None, "") else date.fromisoformat(str(value))
