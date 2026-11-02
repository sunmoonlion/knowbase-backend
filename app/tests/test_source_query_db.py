"""Supplier responses have exact query identities and remain owner-scoped."""

from dataclasses import replace

import pytest
from test_durable_delivery_db import db as db  # noqa: F401
from test_user_resolution_db import World

from app.domain.user_data import UserDataError
from app.domain.user_resolution import (
    DescriptionKey,
    NeedSpec,
    PeriodRule,
    expand,
    need_from,
    query_json,
)


def need(question="synthetic annual report"):
    return NeedSpec(
        key="response",
        name="Wind 查询回执",
        doc_type="other_text",
        period=PeriodRule("none"),
        formats=("text",),
        sources=("collected",),
        source_query=query_json(
            {
                "channel": "wind",
                "service": "financial_docs",
                "tool": "get_company_announcements",
                "arguments": {"query": question},
            }
        ),
    ).checked()


def test_query_roundtrip_is_hashable_and_cannot_masquerade_as_report_pdf():
    n = need()
    assert need_from(n.as_dict()) == n
    from test_user_resolution_db import PARAMS

    [wanted] = expand(n, PARAMS)
    assert DescriptionKey.from_dict(wanted.key.as_dict()) == wanted.key
    assert {wanted.key: "stable"}[wanted.key] == "stable"
    for change in (
        {"doc_type": "annual_report"},
        {"formats": ("pdf",)},
        {"reading": "pages"},
    ):
        with pytest.raises(UserDataError):
            replace(n, **change).checked()


async def test_query_resolves_only_its_own_supplier_response_not_nearby_text(db):
    w = World(db)
    n = need()
    initial = await w.resolve([n])
    assert initial.items[0].status == "to_confirm"
    raw = initial.items[0].key.as_dict()
    [receipt] = await w.lib.deliver_collected(
        "knowfeed",
        "wind",
        "synthetic-query",
        "Wind-response.txt",
        b"synthetic provider response",
        [{"subject": "alice", "item_ref": "query:1", "prefill": raw}],
    )
    await w.lib.show("alice", receipt["material_id"], "app:knowbench")
    current = await w.resolver.refresh_mine("alice", "knowbench", initial.id)
    assert current.items[0].status == "resolved"
    assert current.items[0].chosen[0].material_id == receipt["material_id"]
    # A new acquisition of the same natural-language query must be fresh.
    assert (await w.resolve([n])).items[0].status == "to_confirm"
    other = await w.resolve([need("different year or field")])
    assert other.items[0].status == "to_confirm"
    # The evidence is private and cannot become a public disclosure by matching.
    from app.infrastructure.repositories.user_resolution import (
        SqlUserResolutionRepository,
    )

    repo = SqlUserResolutionRepository(lambda: db)
    assert (
        await repo.candidates(
            "bob", "knowbench", initial.items[0].key, public_access=True
        )
        == []
    )
