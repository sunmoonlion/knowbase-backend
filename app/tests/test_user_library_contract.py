"""资料库接口的跨应用契约 `user-data/v2`（SDD 0043，数据第四版）。

真源是 `app/interfaces/schemas/` 下 `user_library.py`、`user_runs.py`、
`user_resolution.py` 的 pydantic 模型；
契约文件由它生成，逐字一致。
改了模型就跑：`UPDATE_CONTRACTS=1 uv run pytest tests/test_user_library_contract.py`，
再看 diff（只加不改；改了已有字段要开 v3）。
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

from pydantic import BaseModel

from app.interfaces.schemas import user_library as models
from app.interfaces.schemas import user_resolution as resolution
from app.interfaces.schemas import user_runs as runs

CONTRACT_DIR = Path(__file__).resolve().parents[3] / "contracts/user-data/v2"
SCHEMA = CONTRACT_DIR / "user-data.schema.json"
MANIFEST = CONTRACT_DIR / "contract-manifest.json"
MODELS: tuple[type[BaseModel], ...] = (
    models.DescriptionIn,
    models.DescriptionRead,
    models.VersionRead,
    models.MaterialRead,
    models.HoldingRead,
    models.ShelfEntryRead,
    models.LicenseIn,
    models.HistoryEntryRead,
    models.VocabularyRead,
    models.UseIn,
    models.UseRead,
    models.BulkUseIn,
    models.BulkUseRead,
    models.AccountIn,
    models.AccountRead,
    models.ExpertIn,
    models.ExpertRead,
    models.PurchaseIn,
    models.PurchaseRead,
    models.PlatformItemIn,
    models.PlatformConsentIn,
    models.SubscriptionScopeIn,
    models.SubscriptionRead,
    models.FreezeIn,
    runs.GrantItemIn,
    runs.RunGrantIn,
    runs.ServiceRunGrantIn,
    runs.SessionReadsIn,
    runs.PinRead,
    runs.GrantItemRead,
    runs.RunGrantRead,
    runs.RunGrantView,
    runs.SessionReadsRead,
    runs.PlatformEndIn,
    runs.ConvertIn,
    runs.ReadEventRead,
    runs.RenditionRead,
    runs.GrantedFormatRead,
    runs.OutputWritten,
    runs.PendingProcessingRead,
    runs.ProcessingGrantIn,
    resolution.PeriodRuleIn,
    resolution.NeedSpecIn,
    resolution.TaskParamsIn,
    resolution.ResolveIn,
    resolution.AddNeedsIn,
    resolution.DescriptionKeyModel,
    resolution.CandidateRead,
    resolution.ResultItemRead,
    resolution.ResolutionRead,
    resolution.LookupRead,
    resolution.ConfirmItemIn,
    resolution.ConfirmIn,
    resolution.BranchRead,
    resolution.ConfirmationRead,
    resolution.ConfirmationCheckRead,
    resolution.PreferenceRead,
)


def generated() -> str:
    defs: dict[str, object] = {}
    for model in MODELS:
        schema = model.model_json_schema(ref_template="#/$defs/{model}")
        defs.update(schema.pop("$defs", {}))
        defs[model.__name__] = schema
    document = {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$id": "https://contracts.sunmoonai.com/knowbase/user-data/v2/user-data.schema.json",
        "title": "knowbase user-data v2",
        "$defs": dict(sorted(defs.items())),
    }
    return json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def manifest_for(schema_text: str) -> str:
    document = {
        "contract": "sunmoonai.knowbase.user-data",
        "major": 2,
        "provider": "knowbase-app",
        "service_consumers": ["knowbench-app", "knowlab-app", "knowmart-app"],
        "files": {
            "user-data.schema.json": hashlib.sha256(schema_text.encode()).hexdigest()
        },
        "compatibility": {
            "requests": "exact-fields-v2",
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
