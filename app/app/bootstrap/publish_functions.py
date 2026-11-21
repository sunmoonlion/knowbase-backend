"""Publish KnowBase function revisions using the configured service identity."""

import asyncio
import hashlib
import json

from app.application.errors.exceptions import ServiceUnavailableError
from app.application.services.data_functions import TOOLS
from app.infrastructure.external.downstream_service import DownstreamServiceClient
from core.config import get_settings


def object_schema(properties):
    return {
        "type": "object",
        "properties": properties,
        "required": list(properties),
        "additionalProperties": False,
    }


def query_facts_contract():
    nullable_text = {"type": ["string", "null"]}
    source = object_schema(
        {
            "material_id": {"type": "string"},
            "name": {"type": "string"},
            "version_no": {"type": "integer"},
            "reading": {"type": "string"},
            "page": {"type": ["integer", "null"]},
        }
    )
    row = object_schema(
        {
            "ref": {"type": "string"},
            "subject": nullable_text,
            "code": nullable_text,
            "label": {"type": "string"},
            "value": nullable_text,
            "unit": {"type": "string"},
            "currency": {"type": "string"},
            "period_end": nullable_text,
            "duration": nullable_text,
            "consolidation": nullable_text,
            "source": source,
        }
    )
    operation = TOOLS["query_facts"]
    return {
        "description": operation["description"],
        "operation": "query_facts",
        "scenes": ["incubation", "native", "planning", "step"],
        "required_permissions": ["facts.read"],
        "input_schema": operation["inputSchema"],
        "output_schema": object_schema({"facts": {"type": "array", "items": row}}),
    }


async def publish_query_facts(client):
    contract = query_facts_contract()
    expected = {
        "provider": "knowbase",
        "name": "query_facts",
        "version": 1,
        "contract": contract,
    }
    encoded = json.dumps(
        expected, sort_keys=True, ensure_ascii=False, separators=(",", ":")
    ).encode()
    expected["digest"] = "sha256:" + hashlib.sha256(encoded).hexdigest()
    path = "/api/internal/v1/method-skill-center/functions/query_facts/1"
    # The immutable PUT can be repeated when a response is lost.
    for delay in (0.25, 0.5, None):
        try:
            actual = await client.request_json(
                "PUT", path, operation_id=expected["digest"], json_body=contract
            )
        except ServiceUnavailableError:
            if delay is None:
                raise
            await asyncio.sleep(delay)
            continue
        if actual != expected:
            raise ValueError("KnowBench returned a different function revision")
        return expected["digest"]


async def main():
    settings = get_settings()
    if settings.downstream_scope.strip() != "knowbench:function-publish":
        raise ValueError(
            "Expected a knowbench:function-publish service identity"
        )
    print(await publish_query_facts(DownstreamServiceClient(settings)))


if __name__ == "__main__":
    asyncio.run(main())
