"""Publish KnowBase function revisions using the configured service identity."""

import asyncio

from app.application.errors.exceptions import ServiceUnavailableError
from app.application.services.data_query_revisions import (
    REGISTERED_QUERIES,
    function_revision,
)
from app.infrastructure.external.downstream_service import DownstreamServiceClient
from core.config import get_settings


async def publish_query(client, name):
    expected = function_revision(name)
    contract = expected["contract"]
    path = f"/api/internal/v1/method-skill-center/functions/{name}/1"
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
    client = DownstreamServiceClient(settings)
    for name in REGISTERED_QUERIES:
        print(name, await publish_query(client, name))


if __name__ == "__main__":
    asyncio.run(main())
