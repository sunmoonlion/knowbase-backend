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
from app.domain.user_resolution import NEED_KEY
from app.domain.vocabulary import VocabularyError, subject_kind

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


FactKey = tuple[str, date]  # （科目编码，期末日）


@dataclass(frozen=True)
class Picked:
    """按披露版本挑出来的数：每个（科目，期末日）一个；挑不出的列在 `undecided`，
    一个也没有的列在 `missing`。"""

    facts: dict[FactKey, FactRow]
    undecided: tuple[FactKey, ...]
    missing: tuple[FactKey, ...]


def pick(need: FactNeed, found: list[DatedFact]) -> Picked:
    """同一期的同一个数出现在几份报告里时，按披露版本挑一份：当期原始披露取最早披露的，
    最新调整后取最晚的。

    挑不出就不挑（不猜）：几份报告里有没记披露日的，或最早 / 最晚的那一天
    有两份不同的数。
    只看口径对得上的行（主体、报表范围、覆盖时长都相同）。
    """
    wanted = {(code, end) for code in need.codes for end in period_ends(need)}
    groups: dict[FactKey, list[DatedFact]] = {}
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
    facts: dict[FactKey, FactRow] = {}
    undecided: list[FactKey] = []
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
