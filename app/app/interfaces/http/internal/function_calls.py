"""Direct execution of a pinned KnowBase function using an invocation-scoped ticket."""

import hashlib
import json
import time
from decimal import Decimal
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException, Response
from fastapi.encoders import jsonable_encoder
from joserfc import jwt
from joserfc.errors import JoseError
from joserfc.jwk import ECKey
from jsonschema import Draft202012Validator
from pydantic import BaseModel, ConfigDict, Field

from app.application.services.data_functions import (
    DataFunctionError,
    DataFunctions,
    DataToken,
)
from app.application.services.data_query_revisions import (
    REGISTERED_QUERIES,
    function_revision,
)
from app.domain.user_data import UserDataError
from app.interfaces.http.internal.host_guard import internal_host_only
from core.config import Settings


class Invocation(BaseModel):
    model_config = ConfigDict(extra="forbid")
    call_id: UUID
    digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    arguments: dict[str, Any]


def build_function_router(settings: Settings, functions: DataFunctions):
    router = APIRouter(
        prefix="/internal/v1/functions", dependencies=[Depends(internal_host_only)]
    )
    queries = {}
    for name in REGISTERED_QUERIES:
        revision = function_revision(name)
        contract = revision["contract"]
        queries[name] = (
            revision,
            Draft202012Validator(contract["input_schema"]),
            Draft202012Validator(contract["output_schema"]),
        )
    key = (
        ECKey.import_key(settings.knowbase_mcp_jwt_public_key)
        if settings.knowbase_mcp_jwt_public_key
        else None
    )

    @router.post("/{name}/1")
    async def invoke(
        name: str,
        body: Invocation,
        response: Response,
        authorization: Annotated[str | None, Header()] = None,
    ):
        scheme, _, bearer = (authorization or "").partition(" ")
        if not key or scheme.lower() != "bearer" or not bearer:
            raise HTTPException(401, "Function ticket required")
        try:
            claims = jwt.decode(bearer, key, algorithms=["ES256"]).claims
        except JoseError as exc:
            raise HTTPException(401, "Invalid function ticket") from exc
        issuer = settings.knowbase_mcp_jwt_issuer
        if (
            not issuer
            or claims.get("iss") != issuer
            or claims.get("aud") != "knowbase-function"
            or claims.get("azp") != "knowbench"
            or not isinstance(claims.get("exp"), int)
            or claims["exp"] <= time.time()
            or not isinstance(claims.get("sub"), str)
            or not claims["sub"]
            or not isinstance(claims.get("sandbox"), str)
            or not claims["sandbox"]
        ):
            raise HTTPException(401, "Invalid function ticket")
        if name not in queries:
            raise HTTPException(404, "Unknown function revision")
        revision, input_check, output_check = queries[name]
        args = body.arguments
        try:
            payload = json.dumps(
                args,
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
                allow_nan=False,
            ).encode()
        except (ValueError, RecursionError) as exc:
            raise HTTPException(422, "Arguments must be finite JSON") from exc
        if (
            claims.get("call_id") != str(body.call_id)
            or claims.get("function_digest") != body.digest
            or claims.get("arguments_digest") != hashlib.sha256(payload).hexdigest()
        ):
            raise HTTPException(403, "Ticket does not cover this invocation")
        if body.digest != revision["digest"]:
            raise HTTPException(409, "Function revision changed")
        scope_count = int("run" in args) + int("session" in args)
        if (
            args.get("scope") not in ("market", "run")
            or scope_count != (0 if args.get("scope") == "market" else 1)
            or not input_check.is_valid(args)
            or args.keys() - revision["contract"]["input_schema"]["properties"].keys()
        ):
            raise HTTPException(422, "Invalid query scope or arguments")
        try:
            token = DataToken(
                claims["sub"],
                "knowbench",
                claims["sandbox"],
                frozenset({name}),
                function_call_id=str(body.call_id),
            )
            output = jsonable_encoder(
                await functions.invoke(token, name, args),
                custom_encoder={Decimal: str},
            )
        except UserDataError as exc:
            raise HTTPException(
                exc.status, exc.message, headers={"X-KnowBase-Refusal": "resource"}
            ) from exc
        except DataFunctionError as exc:
            raise HTTPException(
                422, str(exc), headers={"X-KnowBase-Refusal": "resource"}
            ) from exc
        except (ValueError, TypeError) as exc:
            raise HTTPException(422, "Invalid data query") from exc
        if not output_check.is_valid(output):
            raise HTTPException(502, "Function output differs from its contract")
        response.headers["Cache-Control"] = "no-store"
        return {"call_id": body.call_id, "digest": body.digest, "output": output}

    return router
