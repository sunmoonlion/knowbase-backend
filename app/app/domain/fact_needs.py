"""要哪些数（SDD 0051 第 2.1、2.2 节，所有者 2026-11-07 定）。

和「要哪份资料」并列的另一种需求：只说要什么——主体、期间、科目表编码、口径——
不写渠道，也不写怎么取（「需求不能决定取法，到了供给端才分」）。knowbase 先在
标准化层里找；没有的交 knowfeed 按渠道先后去取，取回来照样进标准化层。

口径三项：报表范围（合并 / 母公司）、覆盖时长（全年 / 半年 / 单季 / 年初至今）、
披露版本。
披露版本不写时取**当期原始披露**（同一期的数会出现在后来的报告里，上年同期数被调整过；
「当期原始披露」取首次出现它的那份报告，「最新调整后」取最后一份）。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from typing import Literal

from app.domain.chart_of_accounts import CHART
from app.domain.user_data import refused
from app.domain.user_facts import (
    CONSOLIDATIONS,
    Consolidation,
    Duration,
    FactRow,
)
from app.domain.vocabulary import VocabularyError, subject_kind

FACTS_KIND = "facts"  # 描述字典里 `kind` 的值；不带 `kind` 的仍是「要哪份资料」
# 同 user_resolution.NEED_KEY（不 import，免得两个模块互相引用）
NEED_KEY = re.compile(r"^[a-z][a-z0-9_]{0,39}$")
Disclosure = Literal["first", "latest"]
DISCLOSURES: tuple[Disclosure, ...] = ("first", "latest")
# 数据需求的覆盖时长（时点数「instant」是资产负债表的期末数，按期末日要，也收）
FACT_DURATIONS: tuple[Duration, ...] = ("year", "half", "quarter", "ytd", "instant")
MAX_CODES = 30
MAX_PERIODS = 12
# 各覆盖时长的期末月、日（单季、年初至今按季末）
_YEAR_END = (12, 31)
_HALF_END = (6, 30)
_QUARTER_ENDS = ((3, 31), (6, 30), (9, 30), (12, 31))


@dataclass(frozen=True)
class FactNeed:
    """一项「要哪些数」：这家公司、这几期、这几个科目、这个口径。"""

    key: str
    name: str
    subject: str
    start: date
    end: date
    codes: tuple[str, ...]
    consolidation: Consolidation = "consolidated"
    duration: Duration = "year"
    disclosure: Disclosure = "first"
    required: bool = True
    description: str = ""

    def checked(self) -> FactNeed:
        if not NEED_KEY.fullmatch(self.key):
            raise refused("need_key_invalid", "need key is invalid")
        if not self.name.strip() or len(self.name) > 80 or len(self.description) > 500:
            raise refused("need_name_invalid", "need name is invalid")
        try:
            subject_kind(self.subject)
        except VocabularyError as exc:
            raise refused("subject_invalid", str(exc)) from None
        if not self.codes or len(self.codes) > MAX_CODES:
            raise refused("codes_invalid", f"list 1 to {MAX_CODES} chart codes")
        if len(set(self.codes)) != len(self.codes):
            raise refused("codes_invalid", "list each chart code once")
        unknown = [c for c in self.codes if c not in CHART]
        if unknown:
            raise refused("code_invalid", f"unknown chart code {unknown[0]}")
        if self.consolidation not in CONSOLIDATIONS:
            raise refused("consolidation_invalid", "consolidation is invalid")
        if self.duration not in FACT_DURATIONS:
            raise refused("duration_invalid", "duration is invalid")
        if self.disclosure not in DISCLOSURES:
            raise refused("disclosure_invalid", "disclosure is first or latest")
        if self.start > self.end:
            raise refused("period_invalid", "start is after end")
        if not period_ends(self):
            raise refused("period_invalid", "no period ends inside start..end")
        if len(period_ends(self)) > MAX_PERIODS:
            raise refused("period_invalid", f"at most {MAX_PERIODS} periods")
        return self

    # 和「要哪份资料」的 NeedSpec 同名的几项，解析与确认按它们走
    reading = "table"  # 数在表格读法的事实行里
    source_query = None

    def as_dict(self) -> dict[str, object]:
        return {
            "kind": FACTS_KIND,
            "key": self.key,
            "name": self.name,
            "subject": self.subject,
            "start": self.start.isoformat(),
            "end": self.end.isoformat(),
            "codes": list(self.codes),
            "consolidation": self.consolidation,
            "duration": self.duration,
            "disclosure": self.disclosure,
            "required": self.required,
            "description": self.description,
        }


def fact_need_from(data: dict[str, object]) -> FactNeed:
    return FactNeed(
        key=str(data["key"]),
        name=str(data["name"]),
        subject=str(data["subject"]),
        start=date.fromisoformat(str(data["start"])),
        end=date.fromisoformat(str(data["end"])),
        codes=tuple(str(c) for c in data["codes"]),  # type: ignore[union-attr]
        consolidation=data.get("consolidation") or "consolidated",  # type: ignore[arg-type]
        duration=data.get("duration") or "year",  # type: ignore[arg-type]
        disclosure=data.get("disclosure") or "first",  # type: ignore[arg-type]
        required=bool(data.get("required", True)),
        description=str(data.get("description") or ""),
    )


@dataclass(frozen=True)
class FactKey:
    """展开出的一条：这家公司这一期这几个科目、这个口径。交 knowfeed 的需求项照它写，
    knowfeed 决定找哪个渠道、怎么取（需求不写取法）。"""

    subject: str
    period_end: date
    codes: tuple[str, ...]
    consolidation: Consolidation = "consolidated"
    duration: Duration = "year"
    disclosure: Disclosure = "first"

    def checked(self) -> FactKey:
        try:
            subject_kind(self.subject)
        except VocabularyError as exc:
            raise refused("subject_invalid", str(exc)) from None
        if (
            not self.codes
            or len(self.codes) > MAX_CODES
            or len(set(self.codes)) != len(self.codes)
            or any(c not in CHART for c in self.codes)
        ):
            raise refused("codes_invalid", "list known chart codes once each")
        if (
            self.consolidation not in CONSOLIDATIONS
            or self.duration not in FACT_DURATIONS
            or self.disclosure not in DISCLOSURES
        ):
            raise refused("basis_invalid", "consolidation, duration or disclosure")
        return self

    def as_dict(self) -> dict[str, object]:
        return {
            "kind": FACTS_KIND,
            "subject": self.subject,
            "period_end": self.period_end.isoformat(),
            "codes": list(self.codes),
            "consolidation": self.consolidation,
            "duration": self.duration,
            "disclosure": self.disclosure,
        }

    @staticmethod
    def from_dict(data: dict[str, object]) -> FactKey:
        if data.get("kind") != FACTS_KIND:
            raise refused("key_invalid", "not a facts key")
        return FactKey(
            subject=str(data["subject"]),
            period_end=date.fromisoformat(str(data["period_end"])),
            codes=tuple(str(c) for c in data["codes"]),  # type: ignore[union-attr]
            consolidation=data.get("consolidation") or "consolidated",  # type: ignore[arg-type]
            duration=data.get("duration") or "year",  # type: ignore[arg-type]
            disclosure=data.get("disclosure") or "first",  # type: ignore[arg-type]
        )

    def need_for(self, need: FactNeed) -> FactNeed:
        """只看这一期的那份需求（挑数按它）。"""
        return FactNeed(
            key=need.key,
            name=need.name,
            subject=self.subject,
            start=self.period_end,
            end=self.period_end,
            codes=self.codes,
            consolidation=self.consolidation,
            duration=self.duration,
            disclosure=self.disclosure,
            required=need.required,
        )


def fact_keys(need: FactNeed) -> list[FactSlot]:
    """一项「要哪些数」展开成一期一条（新的期在前，同「要哪份资料」）。"""
    return [
        FactKey(
            subject=need.subject,
            period_end=end,
            codes=need.codes,
            consolidation=need.consolidation,
            duration=need.duration,
            disclosure=need.disclosure,
        )
        for end in reversed(period_ends(need))
    ]


def period_ends(need: FactNeed) -> tuple[date, ...]:
    """start..end 之间各期的期末日，按覆盖时长：全年 12-31、半年 6-30、
    单季与年初至今按季末、时点数按年末（资产负债表要别的时点另写单季）。"""
    if need.duration in ("year", "instant"):
        ends = [_YEAR_END]
    elif need.duration == "half":
        ends = [_HALF_END]
    else:
        ends = list(_QUARTER_ENDS)
    out = [
        date(year, month, day)
        for year in range(need.start.year, need.end.year + 1)
        for month, day in ends
        if need.start <= date(year, month, day) <= need.end
    ]
    return tuple(out)


@dataclass(frozen=True)
class DatedFact:
    """一个数和它出自的那份报告是什么时候披露的（`disclosed_on`，资料上记的）。"""

    row: FactRow
    disclosed_on: date | None


FactSlot = tuple[str, date]  # （科目编码，期末日）


@dataclass(frozen=True)
class Picked:
    """按披露版本挑出来的数：每个（科目，期末日）一个；挑不出的列在 `undecided`，
    一个也没有的列在 `missing`。"""

    facts: dict[FactSlot, FactRow]
    undecided: tuple[FactSlot, ...]
    missing: tuple[FactSlot, ...]


def pick(need: FactNeed, found: list[DatedFact]) -> Picked:
    """同一期的同一个数出现在几份报告里时，按披露版本挑一份：当期原始披露取最早披露的，
    最新调整后取最晚的。

    挑不出就不挑（不猜）：几份报告里有没记披露日的，或最早 / 最晚的那一天
    有两份不同的数。
    只看口径对得上的行（主体、报表范围、覆盖时长都相同）。
    """
    wanted = {(code, end) for code in need.codes for end in period_ends(need)}
    groups: dict[FactSlot, list[DatedFact]] = {}
    for item in found:
        row = item.row
        if (
            row.layer != "standard"
            or row.code is None
            or row.period_end is None
            or row.subject != need.subject
            or row.consolidation != need.consolidation
            or row.duration != need.duration
        ):
            continue
        key = (row.code, row.period_end)
        if key in wanted:
            groups.setdefault(key, []).append(item)
    facts: dict[FactSlot, FactRow] = {}
    undecided: list[FactSlot] = []
    for key, items in groups.items():
        materials = {i.row.material_id for i in items}
        if len(materials) == 1:
            facts[key] = items[0].row
            continue
        if any(i.disclosed_on is None for i in items):
            undecided.append(key)
            continue
        days = [i.disclosed_on for i in items if i.disclosed_on is not None]
        edge = min(days) if need.disclosure == "first" else max(days)
        at_edge = [i for i in items if i.disclosed_on == edge]
        if len({i.row.value for i in at_edge}) != 1:
            undecided.append(key)
            continue
        facts[key] = at_edge[0].row
    missing = sorted(wanted - facts.keys() - set(undecided))
    return Picked(
        facts=facts, undecided=tuple(sorted(undecided)), missing=tuple(missing)
    )
