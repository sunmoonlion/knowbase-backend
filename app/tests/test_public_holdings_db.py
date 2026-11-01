"""WP-3：空集证据不能由错期、私有资料或非法查询伪造。"""

from __future__ import annotations

import uuid
from dataclasses import replace

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import update
from test_durable_delivery_db import db as db
from test_public_originals_db import MAOTAI, REPORT_2025
from test_public_originals_db import open_world as open_world
from test_user_data import CSV

from app.application.errors.exceptions import AppException
from app.domain.user_data import UserDataError
from app.infrastructure.models.user_data import UserDataItem
from app.interfaces.endpoints.user_data_admin_routes import (
    get_user_library_service,
    materials_router,
    require_knowbase_admin,
)
from app.interfaces.errors.exception_handlers import register_exception_handlers


async def lookup(world, period="2025", subject=MAOTAI, doc_type="annual_report"):
    return await world.lib.public_holdings(
        subject=subject, doc_type=doc_type, period=period
    )


async def test_absence_then_current_version_with_query_time(open_world):
    at, found = await lookup(open_world)
    assert at == open_world.lib.now()
    assert found == []
    material, version, _ = await open_world.deliver()
    _, found = await lookup(open_world)
    assert [(m.material_id, m.content_version, m.digest) for m in found] == [
        (material.id, version.version_no, version.sha256)
    ]
    _, revised, _ = await open_world.deliver(content=CSV + b"\n")
    _, found = await lookup(open_world)
    assert [(m.content_version, m.digest) for m in found] == [
        (revised.version_no, revised.sha256)
    ]


async def test_period_is_exact_and_not_disclosure_year(open_world):
    await open_world.deliver()  # 2025 年报于 2026 年披露
    assert (await lookup(open_world, "2026"))[1] == []
    assert (await lookup(open_world, "2025-01-01/2025-12-31"))[1]
    assert (await lookup(open_world, "2025-01-02/2025-12-31"))[1] == []
    assert (await lookup(open_world, doc_type="q1_report"))[1] == []
    assert (await lookup(open_world, subject="300750.SZ"))[1] == []


@pytest.mark.parametrize("license_", ["self", "via_expert", "sale"])
async def test_private_upload_never_counts_as_public_original(open_world, license_):
    await open_world.lib.upload(
        "alice",
        "private.csv",
        CSV,
        description=REPORT_2025,
        confirm=True,
        license_=license_,
        attest=True,
    )
    assert (await lookup(open_world))[1] == []


@pytest.mark.parametrize("field", ["deleted_at", "disabled_at", "purged_at"])
async def test_unavailable_public_original_is_excluded(open_world, db, field):
    material, _, _ = await open_world.deliver()
    async with db() as session:
        await session.execute(
            update(UserDataItem)
            .where(UserDataItem.id == uuid.UUID(material.id))
            .values(**{field: open_world.lib.now()})
        )
        await session.commit()
    assert (await lookup(open_world))[1] == []


@pytest.mark.parametrize(
    "period",
    ["", "202x", "2025Q7", "0000", "2025-02-30/2025-12-31", "2025-12-31/2025-01-01"],
)
async def test_invalid_period_is_an_error_not_absence(open_world, period):
    with pytest.raises(UserDataError, match=".") as caught:
        await lookup(open_world, period)
    assert caught.value.code == "holdings_query_invalid"


async def test_admin_http_contract_and_authorization(open_world):
    app = FastAPI()
    app.include_router(materials_router, prefix="/api")
    register_exception_handlers(app)
    allowed = False

    def admin():
        if not allowed:
            raise AppException(code="forbidden", status_code=403, msg="admin required")
        return object()

    app.dependency_overrides[require_knowbase_admin] = admin
    app.dependency_overrides[get_user_library_service] = lambda: open_world.lib
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        path = "/api/admin/v1/materials/holdings"
        params = {"subject": MAOTAI, "doc_type": "annual_report", "period": "2025"}
        assert (await client.get(path, params=params)).status_code == 403
        allowed = True
        response = await client.get(path, params=params)
        assert response.status_code == 200, response.text
        assert set(response.json()) == {"queried_at", "chart_version", "matches"}
        assert response.json()["matches"] == []
        assert (
            await client.get(path, params={**params, "period": "invalid"})
        ).status_code == 422
        assert (
            await client.get(path, params={**params, "doc_type": "not_a_type"})
        ).status_code == 422
        assert (
            await client.get(
                path, params={"subject": MAOTAI, "doc_type": "annual_report"}
            )
        ).status_code == 422


async def test_summary_does_not_prove_full_report_collected(open_world):
    await open_world.lib.deliver_public(
        "knowfeed",
        "cninfo",
        "summary-2025",
        "摘要.csv",
        CSV,
        replace(REPORT_2025, edition="summary"),
    )
    assert (await lookup(open_world))[1] == []
    _, summaries = await open_world.lib.public_holdings(
        subject=MAOTAI, doc_type="annual_report", period="2025", edition="summary"
    )
    assert len(summaries) == 1
