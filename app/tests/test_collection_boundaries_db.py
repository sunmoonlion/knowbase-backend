"""交付与订阅边界用真实库验证，防止类型修复掩盖权限或重试错误。"""

from __future__ import annotations

import json
from datetime import date, timedelta

import pytest
from test_durable_delivery_db import db as db
from test_library_web_http import INTERNAL
from test_library_web_http import api as api
from test_user_data import CSV, Clock
from test_user_library_db import library

from app.domain.user_data import UserDataError
from app.domain.user_library import SubscriptionScope


async def test_expired_subscription_returns_domain_rejection(db):
    clock = Clock()
    lib = library(db, clock=clock)
    await lib.subscribe(
        "alice",
        "test-sub",
        1,
        SubscriptionScope(
            by="description",
            starts_on=clock.now.date(),
            ends_on=clock.now.date() + timedelta(days=1),
            purpose="测试",
            subjects=("600519.SH",),
            doc_types=("annual_report",),
        ),
        "user:knowfeed",
    )
    assert (await lib.live_subscription("knowfeed", "alice", "test-sub", 1)).id
    clock.now += timedelta(days=2)
    with pytest.raises(UserDataError) as caught:
        await lib.live_subscription("knowfeed", "alice", "test-sub", 1)
    assert (caught.value.status, caught.value.code) == (403, "subscription_expired")


def params():
    return {
        "channel": "test-source",
        "channel_doc_id": "report-2024",
        "name": "report.csv",
    }


def beneficiaries():
    return [
        {
            "subject": "alice",
            "item_ref": "confirmed:item-1",
            "prefill": {
                "subject": "600519.SH",
                "doc_type": "annual_report",
                "period_start": "2024-01-01",
                "period_end": "2024-12-31",
                "disclosed_on": "2025-04-03",
            },
        }
    ]


async def test_collected_multipart_dates_processing_basis_and_retry(api):
    client, state, services = api
    state["service"] = "knowfeed"
    path = f"{INTERNAL}/service/collected-originals"

    async def send():
        return await client.post(
            path,
            params=params(),
            files={"file": ("report.csv", CSV)},
            data={"beneficiaries": json.dumps(beneficiaries())},
        )

    first = await send()
    assert first.status_code == 201, first.text
    row = first.json()[0]
    assert row["created"] and not row["has_readings"]
    material = await services.library.repo.get_material(row["material_id"])
    assert material.description.period_end == date(2024, 12, 31)
    assert material.owner == "alice" and material.content_origin == "collected"
    uses = await services.library.repo.uses_of("alice", material.id)
    processing = [
        use
        for use in uses
        if use.use.startswith("processing:") and use.withdrawn_at is None
    ]
    assert len(processing) == 1 and processing[0].created_via == "knowfeed"
    second = await send()
    assert second.status_code == 201, second.text
    assert second.json()[0]["material_id"] == material.id
    assert not second.json()[0]["created"]
    # 用户撤回后重放交付，不能把加工同意重新签出来。
    await services.library.repo.withdraw_use(
        "alice", material.id, processing[0].use, services.library.now()
    )
    assert (await send()).status_code == 201
    uses = await services.library.repo.uses_of("alice", material.id)
    assert not any(
        use.use.startswith("processing:") and use.withdrawn_at is None for use in uses
    )


@pytest.mark.parametrize("content", ["{", '"bad"', "[1]", "[]", "{}", "null"])
async def test_invalid_beneficiaries_are_422_not_500(api, content):
    client, state, _ = api
    state["service"] = "knowfeed"
    response = await client.post(
        f"{INTERNAL}/service/collected-originals",
        params=params(),
        files={"file": ("report.csv", CSV)},
        data={"beneficiaries": content},
    )
    assert response.status_code == 422, response.text
    assert response.json()["code"] == "beneficiaries_invalid"


async def test_collected_file_required(api):
    client, state, _ = api
    state["service"] = "knowfeed"
    response = await client.post(
        f"{INTERNAL}/service/collected-originals",
        params=params(),
        files={"beneficiaries": (None, json.dumps(beneficiaries()))},
    )
    assert response.status_code == 422
    assert response.json()["code"] == "file_missing"
