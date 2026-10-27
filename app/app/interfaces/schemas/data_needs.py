"""数据需求声明 `knowbot-data-needs/v2`（SDD 0043 第 3.1 节）：跨应用契约的唯一真源。

作者在 knowlab 写声明模板，knowmart 收清单 v2 时核，knowbench 按它解析、核数据；
三个应用各自照这份写模型，契约锁核到字（类型、格式的取值来自词表，词表一改锁就变）。
"""

from __future__ import annotations

from datetime import date
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

DocType = Literal[
    "annual_report",
    "semiannual_report",
    "q1_report",
    "q3_report",
    "announcement",
    "prospectus",
    "policy",
    "statistics",
    "webpage",
    "other_table",
    "other_text",
]
DataFormat = Literal["pdf", "excel", "csv", "text", "markdown", "html"]


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class DataTable(_Strict):
    """表格类数据要有的一张表与几列（按表头的字认，不按位置）；CSV 不写表名。"""

    sheet: str | None = Field(default=None, min_length=1, max_length=100)
    columns: tuple[Annotated[str, Field(min_length=1, max_length=100)], ...] = Field(
        min_length=1, max_length=50
    )


class PeriodRule(_Strict):
    """固定期间（start、end），最近 N 个完整的报告期（count，可加截至任务日期已披露），
    或不限期间（none，只给不是定期报告的）。"""

    kind: Literal["fixed", "latest", "none"]
    start: date | None = None
    end: date | None = None
    count: int | None = Field(default=None, ge=1, le=10)
    disclosed_by_task_date: bool = False


class DataNeed(_Strict):
    """声明模板里的一样：类型、期间规则、格式、份数、必填、要的读法、来源限制、
    多候选时的优先策略、表格类的表与列；口径与说明只展示。"""

    key: str = Field(pattern=r"^[a-z][a-z0-9_]{0,39}$")
    name: str = Field(min_length=1, max_length=80)
    doc_type: DocType
    period: PeriodRule
    formats: tuple[DataFormat, ...] = Field(min_length=1, max_length=6)
    required: bool = True
    min_count: int = Field(default=1, ge=1, le=200)
    max_count: int = Field(default=1, ge=1, le=200)
    reading: Literal["original", "text", "pages", "table"] = "original"
    sources: tuple[Literal["disclosure", "collected", "upload", "output"], ...] = Field(
        default=(), max_length=4
    )
    priority: Literal["public_first", "upload_first", "newest"] | None = None
    tables: tuple[DataTable, ...] = Field(default=(), max_length=20)
    caliber: str | None = Field(default=None, max_length=200)
    description: str = Field(default="", max_length=500)
