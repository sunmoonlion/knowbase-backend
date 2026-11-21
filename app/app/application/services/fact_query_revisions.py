"""Versioned KnowBase queries backed by the live fact grants."""

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


REGISTERED_QUERIES = ("query_facts", "query_metric")


def fact_row_schema():
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
    return object_schema(
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


def function_revision(name):
    if name not in REGISTERED_QUERIES:
        raise ValueError("Unknown KnowBase query")
    facts = {"type": "array", "items": fact_row_schema()}
    output = {"facts": facts}
    if name == "query_metric":
        text = {"type": "string"}
        nullable = {"type": ["string", "null"]}
        value = object_schema(
            {
                "subject": nullable,
                "period_end": text,
                "duration": text,
                "consolidation": text,
                "value": nullable,
                "inputs": facts,
            }
        )
        output = {
            "metric": text,
            "name": text,
            "formula": text,
            "values": {"type": "array", "items": value},
        }
    operation = TOOLS[name]
    contract = {
        "description": operation["description"],
        "operation": name,
        "scenes": ["incubation", "native", "planning", "step"],
        "required_permissions": ["facts.read"],
        "input_schema": operation["inputSchema"],
        "output_schema": object_schema(output),
    }
    expected = {
        "provider": "knowbase",
        "name": name,
        "version": 1,
        "contract": contract,
    }
    encoded = json.dumps(
        expected, sort_keys=True, ensure_ascii=False, separators=(",", ":")
    ).encode()
    expected["digest"] = "sha256:" + hashlib.sha256(encoded).hexdigest()
    return expected
