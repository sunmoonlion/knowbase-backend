"""要哪些数（SDD 0051 第 2.1、2.2 节）。

怎么写、怎么展开期间、同一个数出现在几份报告里怎么挑。
"""

from dataclasses import replace
from datetime import date
from decimal import Decimal

import pytest

from app.domain.fact_needs import DatedFact, FactNeed, period_ends, pick
from app.domain.user_data import UserDataError
from app.domain.user_facts import FactRow

NEED = FactNeed(
    key="revenue",
    name="营业收入",
    subject="300750.SZ",
    start=date(2023, 1, 1),
    end=date(2025, 12, 31),
    codes=("IS.REVENUE", "IS.NET_PROFIT_PARENT"),
)


def row(code, end, value, material, **kw):
    return FactRow(
        subject=kw.get("subject", "300750.SZ"),
        code=code,
        label=code,
        value=Decimal(value),
        unit="元",
        currency="CNY",
        period_end=end,
        duration=kw.get("duration", "year"),
        consolidation=kw.get("consolidation", "consolidated"),
        material_id=material,
        material_name=material,
        version_no=1,
        rendition_no=1,
        page=kw.get("page"),
        layer="standard",
        ref=f"fact:{material}-{code}-{end}",
    )


Y23, Y24, Y25 = date(2023, 12, 31), date(2024, 12, 31), date(2025, 12, 31)
AR23 = date(2024, 4, 20)  # 2023 年报披露日
AR24 = date(2025, 4, 25)  # 2024 年报披露日（里面也有 2023 年的上年同期数）


def test_defaults_are_consolidated_full_year_first_disclosure():
    need = NEED.checked()
    assert (need.consolidation, need.duration, need.disclosure) == (
        "consolidated",
        "year",
        "first",
    )


@pytest.mark.parametrize(
    "changes,code",
    [
        ({"codes": ()}, "codes_invalid"),
        ({"codes": ("IS.REVENUE", "IS.REVENUE")}, "codes_invalid"),
        ({"codes": ("NOT.A_CODE",)}, "code_invalid"),
        ({"subject": "bad code!"}, "subject_invalid"),
        ({"disclosure": "whatever"}, "disclosure_invalid"),
        ({"consolidation": "group"}, "consolidation_invalid"),
        ({"start": date(2026, 1, 1)}, "period_invalid"),
        ({"start": date(2024, 1, 1), "end": date(2024, 6, 30)}, "period_invalid"),
        ({"key": "Bad Key"}, "need_key_invalid"),
    ],
)
def test_badly_written_needs_are_refused(changes, code):
    with pytest.raises(UserDataError) as exc:
        replace(NEED, **changes).checked()
    assert exc.value.code == code


def test_period_ends_follow_the_duration():
    assert period_ends(NEED) == (Y23, Y24, Y25)
    half = replace(NEED, duration="half")
    assert period_ends(half) == (
        date(2023, 6, 30),
        date(2024, 6, 30),
        date(2025, 6, 30),
    )
    quarter = replace(NEED, duration="quarter", end=date(2023, 12, 31))
    assert period_ends(quarter) == (
        date(2023, 3, 31),
        date(2023, 6, 30),
        date(2023, 9, 30),
        Y23,
    )


def test_one_report_per_number_is_taken_as_is():
    found = [
        DatedFact(row("IS.REVENUE", Y23, "100", "ar23"), AR23),
        DatedFact(row("IS.NET_PROFIT_PARENT", Y23, "10", "ar23"), AR23),
    ]
    picked = pick(NEED, found)
    assert picked.facts[("IS.REVENUE", Y23)].value == Decimal("100")
    assert picked.undecided == ()
    assert ("IS.REVENUE", Y24) in picked.missing and len(picked.missing) == 4


def test_a_restated_number_picks_by_disclosure_version():
    """2023 年的数：2023 年报里是 100，2024 年报的上年同期改成 98。"""
    found = [
        DatedFact(row("IS.REVENUE", Y23, "100", "ar23"), AR23),
        DatedFact(row("IS.REVENUE", Y23, "98", "ar24"), AR24),
    ]
    assert pick(NEED, found).facts[("IS.REVENUE", Y23)].value == Decimal("100")
    latest = replace(NEED, disclosure="latest")
    assert pick(latest, found).facts[("IS.REVENUE", Y23)].value == Decimal("98")


def test_no_guessing_when_reports_cannot_be_ordered():
    """有一份没记披露日，或同一天两份不同的数：不挑，列为挑不出。"""
    undated = [
        DatedFact(row("IS.REVENUE", Y23, "100", "ar23"), AR23),
        DatedFact(row("IS.REVENUE", Y23, "98", "other"), None),
    ]
    assert pick(NEED, undated).undecided == (("IS.REVENUE", Y23),)
    same_day = [
        DatedFact(row("IS.REVENUE", Y23, "100", "ar23"), AR23),
        DatedFact(row("IS.REVENUE", Y23, "99", "copy"), AR23),
    ]
    picked = pick(NEED, same_day)
    assert picked.undecided == (("IS.REVENUE", Y23),)
    assert ("IS.REVENUE", Y23) not in picked.facts
    assert ("IS.REVENUE", Y23) not in picked.missing


def test_rows_with_another_basis_do_not_count():
    """别的主体、母公司报表、单季的行对不上口径，不算找到。"""
    found = [
        DatedFact(row("IS.REVENUE", Y23, "1", "x", subject="600519.SH"), AR23),
        DatedFact(row("IS.REVENUE", Y23, "2", "x", consolidation="parent"), AR23),
        DatedFact(row("IS.REVENUE", Y23, "3", "x", duration="quarter"), AR23),
    ]
    picked = pick(NEED, found)
    assert picked.facts == {} and ("IS.REVENUE", Y23) in picked.missing
