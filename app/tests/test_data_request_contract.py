"""一样数据的描述的跨应用契约 `data-request/v1`（SDD 0043 第 3.1 节，decisions ⑭）。

真源是 `app/interfaces/schemas/data_request.py` 的 `DataRequest` 与词表
`app/domain/vocabulary.py`；契约文件由它们生成，逐字一致。knowbench、knowlab 照这份
写模型，knowfeed 照词表与科目表写副本，用锁核摘要。
改了就跑：`UPDATE_CONTRACTS=1 uv run pytest tests/test_data_request_contract.py`。
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import get_args

import pytest

from app.domain import chart_of_accounts, vocabulary
from app.interfaces.schemas import data_request

CONTRACT_DIR = Path(__file__).resolve().parents[3] / "contracts/data-request/v1"
SCHEMA = CONTRACT_DIR / "data-request.schema.json"
VOCABULARY = CONTRACT_DIR / "vocabulary.json"
CHART = CONTRACT_DIR / "chart-of-accounts.json"
MANIFEST = CONTRACT_DIR / "contract-manifest.json"


def generated_schema() -> str:
    """一样数据：要哪份资料（`DataRequest`）或要哪些数（`FactRequest`，
    SDD 0051 的加法，带 `kind: facts`）。只能对上其中一个：`FactRequest` 要
    `kind`，两边都不收多余的键。"""
    defs: dict[str, object] = {}
    for model in (data_request.DataRequest, data_request.FactRequest):
        schema = model.model_json_schema(ref_template="#/$defs/{model}")
        defs.update(schema.pop("$defs", {}))
        defs[model.__name__] = schema
    document = {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$id": "https://contracts.sunmoonai.com/knowbase/data-request/v1/data-request.schema.json",
        "title": "data request v1",
        "oneOf": [
            {"$ref": "#/$defs/DataRequest"},
            {"$ref": "#/$defs/FactRequest"},
        ],
        "$defs": dict(sorted(defs.items())),
    }
    return json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def generated_vocabulary() -> str:
    return (
        json.dumps(vocabulary.snapshot(), ensure_ascii=False, indent=2, sort_keys=True)
        + "\n"
    )


def generated_chart() -> str:
    return (
        json.dumps(
            chart_of_accounts.snapshot(), ensure_ascii=False, indent=2, sort_keys=True
        )
        + "\n"
    )


def manifest_for(schema_text: str, vocabulary_text: str, chart_text: str) -> str:
    document = {
        "contract": "sunmoonai.knowbase.data-request",
        "major": 1,
        "provider": "knowbase-app",
        "consumers": ["knowbench-app", "knowlab-app", "knowfeed-app"],
        "vocab_version": vocabulary.VOCAB_VERSION,
        "chart_version": chart_of_accounts.CHART_VERSION,
        "files": {
            "data-request.schema.json": hashlib.sha256(
                schema_text.encode()
            ).hexdigest(),
            "vocabulary.json": hashlib.sha256(vocabulary_text.encode()).hexdigest(),
            "chart-of-accounts.json": hashlib.sha256(chart_text.encode()).hexdigest(),
        },
        "compatibility": {
            "consumers": "same-models-locked-by-sha256",
            "breaking_change": "new-major-and-dual-version-window",
        },
    }
    return json.dumps(document, ensure_ascii=False, indent=2) + "\n"


def test_enumerations_follow_the_vocabulary():
    assert set(get_args(data_request.DocType)) == set(vocabulary.DOC_TYPES) | set(
        vocabulary.DOC_CLASSES
    )
    assert not set(vocabulary.DOC_TYPES) & set(vocabulary.DOC_CLASSES)
    assert set(get_args(data_request.DataFormat)) == set(vocabulary.FORMATS)
    assert set(get_args(data_request.Edition)) == set(vocabulary.EDITIONS)
    assert set(get_args(data_request.Topic)) == set(vocabulary.TOPICS)
    assert get_args(data_request.Industry) == vocabulary.INDUSTRIES


def test_contract_files_are_generated_from_the_models():
    schema_text, vocabulary_text = generated_schema(), generated_vocabulary()
    chart_text = generated_chart()
    manifest = manifest_for(schema_text, vocabulary_text, chart_text)
    if os.environ.get("UPDATE_CONTRACTS") == "1":
        CONTRACT_DIR.mkdir(parents=True, exist_ok=True)
        SCHEMA.write_text(schema_text)
        VOCABULARY.write_text(vocabulary_text)
        CHART.write_text(chart_text)
        MANIFEST.write_text(manifest)
    assert SCHEMA.read_text() == schema_text
    assert VOCABULARY.read_text() == vocabulary_text
    assert CHART.read_text() == chart_text
    assert MANIFEST.read_text() == manifest


def test_a_request_is_either_a_document_or_facts_never_both():
    """SDD 0051：要哪些数是加法；旧的要哪份资料照样过，混写的不过。"""
    import jsonschema

    schema = json.loads(SCHEMA.read_text())
    document = {
        "key": "annual",
        "name": "年报",
        "subject": "300750.SZ",
        "doc_type": "annual_report",
        "period": {"kind": "latest", "count": 3},
        "formats": ["pdf"],
    }
    facts = {
        "kind": "facts",
        "key": "revenue",
        "name": "营业收入",
        "subject": "300750.SZ",
        "start": "2023-01-01",
        "end": "2025-12-31",
        "codes": ["IS.REVENUE"],
    }
    jsonschema.validate(document, schema)
    jsonschema.validate(facts, schema)
    data_request.DataRequest.model_validate(document)
    assert data_request.FactRequest.model_validate(facts).disclosure == "first"
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate({**facts, "doc_type": "annual_report"}, schema)
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate({**document, "kind": "facts"}, schema)
