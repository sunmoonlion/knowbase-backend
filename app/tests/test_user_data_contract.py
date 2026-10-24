"""用户资料接口的跨应用契约 `user-data/v1`（SDD 0042）。

真源是 `app/interfaces/schemas/user_data.py` 的 pydantic 模型；
契约文件由它生成，逐字一致。
改了模型就跑：`UPDATE_CONTRACTS=1 uv run pytest tests/test_user_data_contract.py`，
再看 diff（只加不改；改了已有字段要开 v2）。
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

from pydantic import BaseModel

from app.interfaces.schemas import user_data as models

CONTRACT_DIR = Path(__file__).resolve().parents[3] / "contracts/user-data/v1"
SCHEMA = CONTRACT_DIR / "user-data.schema.json"
MANIFEST = CONTRACT_DIR / "contract-manifest.json"
MODELS: tuple[type[BaseModel], ...] = (
    models.PinIn,
    models.ItemRead,
    models.ConsentCreate,
    models.ConsentRead,
    models.UserGrantCreate,
    models.TestGrantCreate,
    models.GrantRead,
    models.GrantedItem,
    models.GrantView,
    models.FormatRead,
    models.ItemCheck,
    models.PurchaseCreate,
)


def generated() -> str:
    defs: dict[str, object] = {}
    for model in MODELS:
        schema = model.model_json_schema(ref_template="#/$defs/{model}")
        defs.update(schema.pop("$defs", {}))
        defs[model.__name__] = schema
    document = {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$id": "https://contracts.sunmoonai.com/knowbase/user-data/v1/user-data.schema.json",
        "title": "knowbase user-data v1",
        "$defs": dict(sorted(defs.items())),
    }
    return json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def manifest_for(schema_text: str) -> str:
    document = {
        "contract": "sunmoonai.knowbase.user-data",
        "major": 1,
        "provider": "knowbase-app",
        "service_consumers": ["knowbench-app", "knowlab-app", "knowmart-app"],
        "files": {
            "user-data.schema.json": hashlib.sha256(schema_text.encode()).hexdigest()
        },
        "compatibility": {
            "requests": "exact-fields-v1",
            "responses": "consumers-must-tolerate-additive-optional-fields",
            "breaking_change": "new-major-and-dual-version-window",
        },
    }
    return json.dumps(document, ensure_ascii=False, indent=2) + "\n"


def test_contract_files_are_generated_from_the_models():
    schema_text = generated()
    if os.environ.get("UPDATE_CONTRACTS") == "1":
        CONTRACT_DIR.mkdir(parents=True, exist_ok=True)
        SCHEMA.write_text(schema_text)
        MANIFEST.write_text(manifest_for(schema_text))
    assert SCHEMA.read_text() == schema_text
    assert MANIFEST.read_text() == manifest_for(schema_text)
