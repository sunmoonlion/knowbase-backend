"""Publish KnowBase function revisions using the configured service identity."""

import asyncio

from app.application.errors.exceptions import ServiceUnavailableError
from app.application.services.query_facts_revision import query_facts_revision
from app.infrastructure.external.downstream_service import DownstreamServiceClient
from core.config import get_settings


async def publish_query_facts(client):
    expected = query_facts_revision()
    contract = expected["contract"]
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
        raise ValueError("Expected a knowbench:function-publish service identity")
    print(await publish_query_facts(DownstreamServiceClient(settings)))


if __name__ == "__main__":
    asyncio.run(main())
