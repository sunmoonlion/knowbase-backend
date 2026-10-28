"""科目表第 2 版：对上 2015 版分类标准的元素，空着的写原因（10-28）。"""

from __future__ import annotations

import re

from app.domain import chart_of_accounts as chart

ELEMENT = re.compile(r"^(cas|ifrs-full):[A-Za-z]+$")


def test_every_general_item_is_matched_or_says_why_not():
    general = [i for i in chart._ITEMS if "general" in i.company_types]
    assert {i.code for i in general} == set(chart.CAS_2015)
    for item in general:
        if item.cas_element is None:
            assert item.cas_note, item.code
        else:
            assert ELEMENT.match(item.cas_element), item.code


def test_bank_items_wait_for_the_bank_expert():
    banks = [i for i in chart._ITEMS if i.company_types == ("bank",)]
    assert banks
    assert all(i.cas_element is None and "⑱" in (i.cas_note or "") for i in banks)


def test_items_new_since_2015_have_no_element():
    for code in (
        "IS.RND",
        "BS.RIGHT_OF_USE",
        "BS.LEASE_LIABILITIES",
        "BS.CONTRACT_LIABILITIES",
        "CF.DEPRECIATION_RIGHT_OF_USE",
    ):
        assert chart.CHART[code].cas_element is None
        assert "2015 版没有" in (chart.CHART[code].cas_note or "")


def test_version_two_adds_depreciation_and_amortization():
    assert chart.CHART_VERSION == 2
    for code in (
        "CF.DEPRECIATION_FIXED_ASSETS",
        "CF.DEPRECIATION_RIGHT_OF_USE",
        "CF.AMORTIZATION_INTANGIBLES",
        "CF.AMORTIZATION_LT_PREPAID",
    ):
        assert chart.CHART[code].statement == "cashflow"
    snapshot = chart.snapshot()
    assert snapshot["cas_taxonomy"] == chart.CAS_TAXONOMY
    assert {"cas_element", "cas_note"} <= set(snapshot["items"][0])
