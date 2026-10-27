"""结构化财务数据（SDD 0047 第四节）：原样层、标准化层、质检。

「抽好的表」是原件某个内容版本的一种读法（`table`）；这个读法版本的内容是一批事实行，
和读法版本一起只增不改，读法版本的摘要按这批行的规范化内容算。

- 原样层：报表上印的每个数一行（科目名原文、页码、行序、单位），用户上传的杂乱表格
  也进这一层（表名、单元格位置照原样）；
- 标准化层：对到科目编码（`chart_of_accounts`）、换算到统一单位的数，记由原样层哪几行
  得来、用的哪条对照规则；认得出才进，认不出不硬套。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Literal

from app.domain.chart_of_accounts import CHART, CHART_VERSION
from app.domain.user_data import refused

Duration = Literal["year", "half", "quarter", "ytd", "instant"]
DURATIONS: tuple[Duration, ...] = ("year", "half", "quarter", "ytd", "instant")
Consolidation = Literal["consolidated", "parent"]
CONSOLIDATIONS: tuple[Consolidation, ...] = ("consolidated", "parent")
# 金额单位换算到「元」；每股、股数、百分比不换
UNIT_FACTORS: dict[str, Decimal] = {
    "元": Decimal(1),
    "千元": Decimal(1_000),
    "万元": Decimal(10_000),
    "百万元": Decimal(1_000_000),
    "亿元": Decimal(100_000_000),
}
OTHER_UNITS = ("元/股", "股", "%")
MAX_ROWS = 20_000
# 恒等式允许的误差：报表四舍五入到元，合计常差几块钱
IDENTITY_TOLERANCE = Decimal(10)


@dataclass(frozen=True)
class RawFact:
    label: str
    value: Decimal | None
    unit: str
    currency: str = "CNY"
    period_end: date | None = None
    duration: str | None = None
    consolidation: str | None = None
    statement_label: str | None = None
    page: int | None = None
    row_no: int | None = None
    sheet: str | None = None
    cell: str | None = None

    def checked(self) -> RawFact:
        if not self.label.strip() or len(self.label) > 200:
            raise refused("fact_invalid", "a raw fact needs its label")
        if self.unit not in UNIT_FACTORS and self.unit not in OTHER_UNITS:
            raise refused("unit_invalid", f"unknown unit {self.unit}")
        if self.duration is not None and self.duration not in DURATIONS:
            raise refused("duration_invalid", "duration is invalid")
        if self.consolidation is not None and self.consolidation not in CONSOLIDATIONS:
            raise refused("consolidation_invalid", "consolidation is invalid")
        return self

    def as_dict(self) -> dict[str, object]:
        return {
            "label": self.label,
            "value": str(self.value) if self.value is not None else None,
            "unit": self.unit,
            "currency": self.currency,
            "period_end": self.period_end.isoformat() if self.period_end else None,
            "duration": self.duration,
            "consolidation": self.consolidation,
            "statement_label": self.statement_label,
            "page": self.page,
            "row_no": self.row_no,
            "sheet": self.sheet,
            "cell": self.cell,
        }


@dataclass(frozen=True)
class StandardFact:
    """标准化层的一个数；`derived_from` 是这批原样行里的下标。"""

    code: str
    value: Decimal
    period_end: date
    duration: str
    consolidation: str
    derived_from: tuple[int, ...]
    rule: str
    currency: str = "CNY"
    page: int | None = None

    def checked(self, raw_count: int) -> StandardFact:
        if self.code not in CHART:
            raise refused("code_invalid", f"unknown chart code {self.code}")
        if self.duration not in DURATIONS:
            raise refused("duration_invalid", "duration is invalid")
        if self.consolidation not in CONSOLIDATIONS:
            raise refused("consolidation_invalid", "consolidation is invalid")
        if not self.derived_from or any(
            not 0 <= i < raw_count for i in self.derived_from
        ):
            raise refused("derived_from_invalid", "derived_from names raw rows")
        if not self.rule or len(self.rule) > 80:
            raise refused("rule_invalid", "say which mapping rule")
        return self

    def as_dict(self) -> dict[str, object]:
        return {
            "code": self.code,
            "value": str(self.value),
            "period_end": self.period_end.isoformat(),
            "duration": self.duration,
            "consolidation": self.consolidation,
            "derived_from": list(self.derived_from),
            "rule": self.rule,
            "currency": self.currency,
            "page": self.page,
        }


def to_yuan(value: Decimal, unit: str) -> Decimal:
    """金额换算到元；不是金额的原样。"""
    return value * UNIT_FACTORS.get(unit, Decimal(1))


def batch_digest(raw: list[RawFact], standard: list[StandardFact]) -> str:
    """这批事实行的摘要：规范化（键排序、紧凑）后 sha256；行的先后算在内。"""
    document = {
        "chart_version": CHART_VERSION,
        "raw": [r.as_dict() for r in raw],
        "standard": [s.as_dict() for s in standard],
    }
    canonical = json.dumps(
        document, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def check_batch(
    raw: list[RawFact], standard: list[StandardFact]
) -> tuple[list[RawFact], list[StandardFact]]:
    if not raw or len(raw) + len(standard) > MAX_ROWS:
        raise refused("facts_invalid", f"give 1 to {MAX_ROWS} rows")
    checked_raw = [r.checked() for r in raw]
    checked = [s.checked(len(raw)) for s in standard]
    keys = [(s.code, s.period_end, s.duration, s.consolidation) for s in checked]
    if len(keys) != len(set(keys)):
        raise refused("facts_duplicate", "one value per code, period and caliber")
    return checked_raw, checked


def quality(standard: list[StandardFact]) -> list[dict[str, object]]:
    """恒等式质检（结果照实记在读法上，不改数）：资产总计 = 负债和所有者权益总计；
    资产总计 = 负债合计 + 所有者权益合计。缺项的不判。"""
    by_key: dict[tuple[date, str], dict[str, Decimal]] = {}
    for s in standard:
        if s.duration == "instant":
            by_key.setdefault((s.period_end, s.consolidation), {})[s.code] = s.value
    results: list[dict[str, object]] = []
    for (period_end, consolidation), values in sorted(by_key.items()):
        checks = [
            (
                "assets_equal_liabilities_and_equity",
                "BS.TOTAL_ASSETS",
                ("BS.TOTAL_LIABILITIES_EQUITY",),
            ),
            (
                "assets_equal_liabilities_plus_equity",
                "BS.TOTAL_ASSETS",
                (
                    "BS.TOTAL_LIABILITIES",
                    "BS.TOTAL_EQUITY",
                ),
            ),
        ]
        for name, left, right in checks:
            if left not in values or any(r not in values for r in right):
                continue
            diff = values[left] - sum((values[r] for r in right), Decimal(0))
            results.append(
                {
                    "check": name,
                    "period_end": period_end.isoformat(),
                    "consolidation": consolidation,
                    "passed": abs(diff) <= IDENTITY_TOLERANCE,
                    "diff": str(diff),
                }
            )
    return results


@dataclass(frozen=True)
class FactRow:
    """查出来的一个数，带出处到页。"""

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


MAX_RESULT_ROWS = 2_000


@dataclass(frozen=True)
class FactQuery:
    """查事实的条件；跨全市场筛选、排序、排名也用它（`value_min`/`value_max`、
    `order` 按第一个科目的值）。"""

    layer: Literal["standard", "raw"] = "standard"
    subjects: tuple[str, ...] = ()
    codes: tuple[str, ...] = ()
    labels: tuple[str, ...] = ()
    period_from: date | None = None
    period_to: date | None = None
    duration: str | None = None
    consolidation: str | None = "consolidated"
    value_min: Decimal | None = None
    value_max: Decimal | None = None
    order: Literal["period", "value_desc", "value_asc"] = "period"
    limit: int = 500

    def checked(self) -> FactQuery:
        if self.layer not in ("standard", "raw"):
            raise refused("layer_invalid", "layer is standard or raw")
        if any(c not in CHART for c in self.codes):
            raise refused("code_invalid", "unknown chart code")
        if self.layer == "raw" and self.codes:
            raise refused("codes_invalid", "raw rows are found by label, not code")
        if self.duration is not None and self.duration not in DURATIONS:
            raise refused("duration_invalid", "duration is invalid")
        if self.consolidation is not None and self.consolidation not in CONSOLIDATIONS:
            raise refused("consolidation_invalid", "consolidation is invalid")
        if (self.value_min is not None or self.value_max is not None) and len(
            self.codes
        ) != 1:
            raise refused("filter_invalid", "value filters need exactly one code")
        if self.order != "period" and len(self.codes) != 1:
            raise refused("order_invalid", "ordering by value needs exactly one code")
        if not 1 <= self.limit <= MAX_RESULT_ROWS:
            raise refused("limit_invalid", f"limit is 1 to {MAX_RESULT_ROWS}")
        if len(self.subjects) > 200 or len(self.codes) > 50 or len(self.labels) > 50:
            raise refused("filter_invalid", "too many filter values")
        return self


@dataclass(frozen=True)
class MetricRow:
    """一个指标值，带参与计算的每个数（各自有出处）。"""

    subject: str | None
    period_end: date
    duration: str
    consolidation: str
    value: Decimal | None
    inputs: tuple[FactRow, ...]


def compute_metric(metric, rows: list[FactRow]) -> list[MetricRow]:
    """按指标定义算：ratio 是分子之和 / 分母之和；growth 是本期 / 上年同期 − 1。
    缺项或分母为零的给 None，照实不补。"""

    def signed(terms: tuple[str, ...], values: dict[str, FactRow]) -> Decimal | None:
        total = Decimal(0)
        for term in terms:
            fact = values.get(term[1:])
            if fact is None or fact.value is None:
                return None
            total += fact.value if term[0] == "+" else -fact.value
        return total

    groups: dict[tuple, dict[str, FactRow]] = {}
    for row in rows:
        if row.code is None or row.period_end is None:
            continue
        key = (row.subject, row.period_end, row.duration, row.consolidation)
        groups.setdefault(key, {})[row.code] = row
    out: list[MetricRow] = []
    for (subject, period_end, duration, consolidation), values in sorted(
        groups.items(), key=lambda kv: (kv[0][0] or "", kv[0][1]), reverse=True
    ):
        if metric.kind == "ratio":
            top = signed(metric.numerator, values)
            bottom = signed(metric.denominator, values)
            value = top / bottom if top is not None and bottom else None
            inputs = tuple(
                values[t[1:]]
                for t in metric.numerator + metric.denominator
                if t[1:] in values
            )
        else:
            try:
                earlier = period_end.replace(year=period_end.year - 1)
            except ValueError:  # 2 月 29 日
                earlier = period_end.replace(year=period_end.year - 1, day=28)
            prior = groups.get((subject, earlier, duration, consolidation), {})
            now, before = (
                signed(metric.numerator, values),
                signed(metric.numerator, prior),
            )
            value = now / before - 1 if now is not None and before else None
            inputs = tuple(
                v[t[1:]]
                for v in (values, prior)
                for t in metric.numerator
                if t[1:] in v
            )
        out.append(
            MetricRow(
                subject, period_end, str(duration), str(consolidation), value, inputs
            )
        )
    return out
