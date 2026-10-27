"""数据需求声明的跨应用契约 `knowbot-data-needs/v2`（SDD 0043 第 3.1 节）。

真源是 `app/interfaces/schemas/data_needs.py` 的 `DataNeed` 与词表
`app/domain/vocabulary.py`；契约文件由它们生成，逐字一致。knowlab、knowmart、
knowbench 各照这份写模型，用锁核摘要。
改了就跑：`UPDATE_CONTRACTS=1 uv run pytest tests/test_data_needs_contract.py`。
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import get_args

from app.domain import chart_of_accounts, vocabulary
from app.interfaces.schemas import data_needs

CONTRACT_DIR = Path(__file__).resolve().parents[3] / "contracts/knowbot-data-needs/v2"
SCHEMA = CONTRACT_DIR / "data-needs.schema.json"
VOCABULARY = CONTRACT_DIR / "vocabulary.json"
CHART = CONTRACT_DIR / "chart-of-accounts.json"
MANIFEST = CONTRACT_DIR / "contract-manifest.json"


def generated_schema() -> str:
    schema = data_needs.DataNeed.model_json_schema(ref_template="#/$defs/{model}")
    defs = schema.pop("$defs", {})
    defs["DataNeed"] = schema
    document = {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$id": "https://contracts.sunmoonai.com/knowbase/knowbot-data-needs/v2/data-needs.schema.json",
        "title": "knowbot data needs v2",
        "$ref": "#/$defs/DataNeed",
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
        "contract": "sunmoonai.knowbase.knowbot-data-needs",
        "major": 2,
        "provider": "knowbase-app",
        "consumers": ["knowlab-app", "knowmart-app", "knowbench-app"],
        "vocab_version": vocabulary.VOCAB_VERSION,
        "chart_version": chart_of_accounts.CHART_VERSION,
        "files": {
            "data-needs.schema.json": hashlib.sha256(schema_text.encode()).hexdigest(),
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
    assert set(get_args(data_needs.DocType)) == set(vocabulary.DOC_TYPES) | set(
        vocabulary.DOC_CLASSES
    )
    assert not set(vocabulary.DOC_TYPES) & set(vocabulary.DOC_CLASSES)
    assert set(get_args(data_needs.DataFormat)) == set(vocabulary.FORMATS)
    assert set(get_args(data_needs.Edition)) == set(vocabulary.EDITIONS)
    assert set(get_args(data_needs.Topic)) == set(vocabulary.TOPICS)
    assert get_args(data_needs.Industry) == vocabulary.INDUSTRIES


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
