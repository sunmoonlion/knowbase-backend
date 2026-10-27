"""事实接口的模型（契约 `user-data/v2` 的一部分，SDD 0047 第四节）。"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

Duration = Literal["year", "half", "quarter", "ytd", "instant"]
Consolidation = Literal["consolidated", "parent"]


class RawFactIn(BaseModel):
    """原样层的一行：报表上印的样子（科目名原文、单位、页码、行序）。"""

    model_config = ConfigDict(extra="forbid")

    label: str = Field(min_length=1, max_length=200)
    value: Decimal | None = None
    unit: str = Field(min_length=1, max_length=8)
    currency: str = Field(default="CNY", min_length=3, max_length=3)
    period_end: date | None = None
    duration: Duration | None = None
    consolidation: Consolidation | None = None
    statement_label: str | None = Field(default=None, max_length=100)
    page: int | None = Field(default=None, ge=1)
    row_no: int | None = Field(default=None, ge=1)
    sheet: str | None = Field(default=None, max_length=100)
    cell: str | None = Field(default=None, max_length=20)


class StandardFactIn(BaseModel):
    """标准化层的一行：科目编码、换算到元的数、由原样层哪几行得来、对照规则。"""

    model_config = ConfigDict(extra="forbid")

    code: str = Field(min_length=1, max_length=64)
    value: Decimal
    period_end: date
    duration: Duration
    consolidation: Consolidation
    derived_from: list[int] = Field(min_length=1, max_length=50)
    rule: str = Field(min_length=1, max_length=80)
    currency: str = Field(default="CNY", min_length=3, max_length=3)
    page: int | None = Field(default=None, ge=1)


class FactsIn(BaseModel):
    """knowfeed 写回抽好的表（一批）。"""

    model_config = ConfigDict(extra="forbid")

    processor_version: str = Field(min_length=1, max_length=40)
    raw: list[RawFactIn] = Field(min_length=1, max_length=20_000)
    standard: list[StandardFactIn] = Field(default_factory=list, max_length=20_000)


class FactQueryIn(BaseModel):
    """查事实；跨全市场筛选、排序、排名按第一个科目（只给一个科目时）。"""

    model_config = ConfigDict(extra="forbid")

    layer: Literal["standard", "raw"] = "standard"
    subjects: list[str] = Field(default_factory=list, max_length=200)
    codes: list[str] = Field(default_factory=list, max_length=50)
    labels: list[str] = Field(default_factory=list, max_length=50)
    period_from: date | None = None
    period_to: date | None = None
    duration: Duration | None = None
    consolidation: Consolidation | None = "consolidated"
    value_min: Decimal | None = None
    value_max: Decimal | None = None
    order: Literal["period", "value_desc", "value_asc"] = "period"
    limit: int = Field(default=500, ge=1, le=2_000)


class RunFactsIn(FactQueryIn):
    """执行的应用查这次运行钉住的那几份。"""

    run_ref: str = Field(min_length=1, max_length=255)
    beneficiary: str = Field(min_length=1, max_length=255)


class MetricQueryIn(FactQueryIn):
    metric: str = Field(min_length=1, max_length=40)


class RunMetricIn(MetricQueryIn):
    run_ref: str = Field(min_length=1, max_length=255)
    beneficiary: str = Field(min_length=1, max_length=255)


class FactRowRead(BaseModel):
    """查出来的一个数，带出处：哪份资料、哪一版、哪个表格读法版本、第几页。"""

    subject: str | None
    code: str | None
    label: str
    value: Decimal | None
    unit: str
    currency: str
    period_end: date | None
    duration: str | None
    consolidation: str | None
    material_id: str
    material_name: str
    version_no: int
    rendition_no: int
    page: int | None
    layer: Literal["standard", "raw"]


class MetricRowRead(BaseModel):
    subject: str | None
    period_end: date
    duration: str
    consolidation: str
    value: Decimal | None
    inputs: list[FactRowRead]


class ChartItemRead(BaseModel):
    code: str
    name: str
    statement: str
    company_types: list[str]
    aliases: list[str]
    cas_element: str | None


class MetricDefinitionRead(BaseModel):
    key: str
    name: str
    kind: str
    description: str


class ChartRead(BaseModel):
    version: int
    company_types: list[str]
    items: list[ChartItemRead]
    metrics: list[MetricDefinitionRead]
