"""本人重核对走实际 HTTP 路由和隔离数据库，不用 refresh_mine 替身。"""

import uuid

import pytest
from sqlalchemy import text
from test_durable_delivery_db import db as db  # noqa: F401
from test_user_library_http import BASE, CSV  # noqa: F401
from test_user_library_http import api as api

from app.domain.user_data import Owner


async def create_list(client):
    response = await client.post(
        f"{BASE}/resolutions",
        json={
            "consumer": "expert:fa-1@2",
            "contract_version": "data-request/v1",
            "needs": [
                {
                    "key": "annual",
                    "name": "年报",
                    "subject": "600519.SH",
                    "doc_type": "annual_report",
                    "period": {"kind": "latest", "count": 2},
                    "formats": ["csv"],
                }
            ],
            "params": {"task_date": "2026-10-27"},
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


async def upload(client, year, content=CSV):
    response = await client.post(
        f"{BASE}/materials",
        params={
            "name": f"maotai-{year}.csv",
            "subject": "600519.SH",
            "doc_type": "annual_report",
            "period_start": f"{year}-01-01",
            "period_end": f"{year}-12-31",
            "disclosed_on": f"{year + 1}-03-28",
            "confirm": "true",
        },
        content=content,
    )
    assert response.status_code == 201, response.text
    return response.json()


async def test_refresh_only_rechecks_gaps_and_preserves_selected_materials(api, db):
    client, state = api
    await client.put(f"{BASE}/account", json={"default_display": True})
    await upload(client, 2025)
    original = await create_list(client)
    path = f"{BASE}/resolutions/{original['resolution_id']}/refresh"
    assert (await client.post(path)).json() == original  # 无变化不制造新修订
    await upload(client, 2024)
    await upload(client, 2025, CSV + b"5,6\n")  # 新候选不能改掉已选版本
    refreshed = await client.post(path)
    assert refreshed.status_code == 200, refreshed.text
    body = refreshed.json()
    assert body["revision"] == 2
    assert body["items"][0] == original["items"][0]
    assert [i["status"] for i in body["items"]] == ["resolved", "resolved"]
    assert [i["key"] for i in body["items"]] == [i["key"] for i in original["items"]]
    assert body["consumer"] == original["consumer"]
    assert (await client.post(path)).json() == body
    state["service"] = "knowbench"
    service = await client.post(
        f"{BASE}/service/resolutions/{original['resolution_id']}/refresh",
        params={"subject": "alice"},
    )
    assert service.json() == body  # 两种身份走同一个重解析实现
    async with db() as session:
        for table in ("user_data_confirmation", "user_data_run_grant"):
            count = await session.scalar(text(f"SELECT count(*) FROM {table}"))
            assert count == 0  # 没有替本人确认或签授权


@pytest.mark.parametrize(
    "owner", [Owner("bob", "knowbench"), Owner("alice", "knowlab")]
)
async def test_refresh_is_scoped_to_both_user_and_application(api, owner):
    client, state = api
    original = await create_list(client)
    state["owner"] = owner
    denied = await client.post(
        f"{BASE}/resolutions/{original['resolution_id']}/refresh",
        params={"subject": "alice"},
    )
    assert (denied.status_code, denied.json()["code"]) == (404, "resolution_not_found")
    state["owner"] = Owner("alice", "knowbench")
    unchanged = await client.get(f"{BASE}/resolutions/{original['resolution_id']}")
    assert unchanged.json() == original
    missing = await client.post(f"{BASE}/resolutions/{uuid.uuid4()}/refresh")
    assert (missing.status_code, missing.json()["code"]) == (
        404,
        "resolution_not_found",
    )
