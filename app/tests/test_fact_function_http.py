"""Direct function calls bind the exact query and re-read the user's live grant."""

import hashlib
import json
from uuid import uuid4

import httpx
from fastapi import FastAPI
from joserfc import jwt
from test_data_mcp import KEY, World, token
from test_durable_delivery_db import db as db

from app.application.services.query_facts_revision import query_facts_revision
from app.domain.public_access import PublicGate
from app.domain.user_library import PUBLIC_OWNER
from app.domain.user_runs import GrantRequestItem, InputPin
from app.interfaces.http.internal.function_calls import build_function_router
from core.config import Settings


async def test_query_facts_ticket_and_live_grant(db):
    world = World(
        db, PublicGate(qualification="listed", qualified=frozenset({"alice"}))
    )
    material, version = await world.publish()
    grant = await world.runs.grant_by_user(
        "alice",
        "knowbench",
        "delegation",
        "trial",
        [
            GrantRequestItem(
                InputPin(material.id, 1, version.sha256), "public", PUBLIC_OWNER
            )
        ],
    )
    app = FastAPI()
    settings = Settings(
        knowbase_mcp_jwt_public_key=KEY.as_pem(private=False).decode(),
        knowbase_mcp_jwt_issuer="knowbench",
    )
    app.include_router(build_function_router(settings, world.functions), prefix="/api")
    request = {
        "call_id": str(uuid4()),
        "digest": query_facts_revision()["digest"],
        "arguments": {"scope": "run", "run": "trial", "codes": ["IS.REVENUE"]},
    }
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app), base_url="https://127.0.0.1"
    ) as http:

        async def send(body, *, signed=None, sub="alice", audience="knowbase-function"):
            bound = signed or body
            args = json.dumps(
                bound["arguments"],
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
            ).encode()
            claims = dict(jwt.decode(token(sub=sub, aud=audience), KEY).claims)
            claims.update(
                call_id=bound["call_id"],
                function_digest=bound["digest"],
                arguments_digest=hashlib.sha256(args).hexdigest(),
            )
            return await http.post(
                "/api/internal/v1/functions/query_facts/1",
                json=body,
                headers={
                    "Authorization": "Bearer "
                    + jwt.encode({"alg": "ES256"}, claims, KEY)
                },
            )

        reply = await send(request)
        assert reply.status_code == 200, reply.text
        assert (
            reply.json()["output"]["facts"][0]["source"]["material_id"] == material.id
        )
        assert reply.json()["output"]["facts"][0]["value"] == "15000000.000000"
        reads = await world.runs.reads(PUBLIC_OWNER, material.id)
        assert any(r["reader"] == "function:" + request["call_id"] for r in reads)
        market = request | {"arguments": {"scope": "market", "codes": ["IS.REVENUE"]}}
        assert (await send(market)).json()["output"]["facts"]
        assert (await send(market, sub="bob")).status_code == 403
        assert (await send(request, audience="knowbase-data")).status_code == 401
        assert (
            await send(request | {"call_id": str(uuid4())}, signed=request)
        ).status_code == 403
        assert (
            await send(request | {"arguments": {"scope": "market"}}, signed=request)
        ).status_code == 403
        assert (
            await send(request | {"digest": "sha256:" + "0" * 64})
        ).status_code == 409
        assert (await send(request, sub="bob")).json()["output"]["facts"] == []
        await world.runs.revoke_by_user("alice", grant.id)
        assert (await send(request)).json()["output"]["facts"] == []
