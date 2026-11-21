"""The exact provider contract served by query_facts version one."""

import hashlib
import json

from app.application.services.data_functions import TOOLS


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


def query_facts_revision():
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
    return expected
