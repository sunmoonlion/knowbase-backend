"""KnowBase publishes a pinned source using the KnowBench-owned contract."""

import hashlib
import json
import os
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

from app.application.errors.exceptions import BadRequestError, ServiceUnavailableError
from app.application.services.fact_query_revisions import function_revision
from app.bootstrap.publish_functions import publish_query

PARENT = Path(__file__).resolve().parents[3]


async def test_lost_response_retries_exact_request_and_checks_revision():
    requests = []

    class Center:
        async def request_json(self, method, path, *, operation_id, json_body):
            requests.append((method, path, operation_id, json_body))
            if len(requests) == 1:
                raise ServiceUnavailableError()
            return {
                "provider": "knowbase",
                "name": "query_facts",
                "version": 1,
                "digest": operation_id,
                "contract": json_body,
            }

    fingerprint = await publish_query(Center(), "query_facts")
    assert requests[0] == requests[1]
    assert requests[0][:3] == (
        "PUT",
        "/api/internal/v1/method-skill-center/functions/query_facts/1",
        fingerprint,
    )
    # Publisher's request/response must conform to the schema locked from KnowBench.
    lock_path = PARENT / "contracts/knowbench-service-functions-v1-lock.json"
    if not lock_path.is_file():
        pytest.skip(
            "Check out the parent contract lock to verify the consumer contract"
        )
    contracts = Path(
        os.environ.get(
            "KNOWBENCH_SERVICE_FUNCTION_SCHEMA_DIR",
            PARENT.parent / "knowbench-app/knowbench-backend/app/contracts",
        )
    )
    lock = json.loads(lock_path.read_text())["sha256"]
    for filename, expected in lock.items():
        path = contracts / filename
        assert hashlib.sha256(path.read_bytes()).hexdigest() == expected
        value = function_revision("query_facts")["contract"]
        if filename == "service-function-revision-v1.json":
            value = {
                "provider": "knowbase",
                "name": "query_facts",
                "version": 1,
                "digest": fingerprint,
                "contract": value,
            }
        Draft202012Validator(json.loads(path.read_text())).validate(value)


@pytest.mark.parametrize("conflict", [True, False])
async def test_publication_conflict_or_wrong_receipt_does_not_retry(conflict):
    class Center:
        calls = 0

        async def request_json(self, *args, **kwargs):
            self.calls += 1
            if conflict:
                raise BadRequestError()
            return {"provider": "knowfeed"}

    center = Center()
    with pytest.raises(BadRequestError if conflict else ValueError):
        await publish_query(center, "query_facts")
    assert center.calls == 1
