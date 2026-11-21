"""Versioned KnowBase queries backed by live data grants."""

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


REGISTERED_QUERIES = ("query_facts", "query_metric", "read_facts", "list_inputs")


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


def input_list_schema():
    text = {"type": "string"}
    nullable = {"type": ["string", "null"]}
    integer = {"type": "integer"}
    item = object_schema(
        {
            "material_id": text,
            "name": nullable,
            "version_no": integer,
            "subject": nullable,
            "doc_type": nullable,
            "period_start": nullable,
            "period_end": nullable,
            "readings": {"type": "array", "items": text},
            "owner": {"enum": ["public", "yours", "author"]},
            "basis": text,
            "grant_revision": integer,
        }
    )
    output_schema = object_schema({"inputs": {"type": "array", "items": item}})
    output_schema["properties"]["run"] = text
    return output_schema


def fact_output_schema(name):
    facts = {"type": "array", "items": fact_row_schema()}
    output = {"facts": facts}
    if name == "read_facts":
        output["not_found"] = {"type": "array", "items": {"type": "string"}}
    elif name == "query_metric":
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
    return object_schema(output)


def function_revision(name):
    if name not in REGISTERED_QUERIES:
        raise ValueError("Unknown KnowBase query")
    output_schema = (
        input_list_schema() if name == "list_inputs" else fact_output_schema(name)
    )
    operation = TOOLS[name]
    contract = {
        "description": operation["description"],
        "operation": name,
        "scenes": ["incubation", "native", "planning", "step"],
        "required_permissions": [
            "inputs.list" if name == "list_inputs" else "facts.read"
        ],
        "input_schema": operation["inputSchema"],
        "output_schema": output_schema,
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
