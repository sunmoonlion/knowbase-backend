"""按口径名查询（0009-semantic 乙段，SEM-07 至 SEM-09）。

用的是 knowfeed 按数据集自述第二版重建的 600009 数据集原件；旧版原件用来验「不支持」的答复。
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import replace
from pathlib import Path

import pytest
from test_knowledge_datasets import (
    TOKEN,
    TOKEN_NO_LIST,
    LocalFiles,
    MemoryRegistry,
    registered,
    registration,
    settings_for,
)
from test_knowledge_datasets import default_dataset as default_dataset

from app.application.services.dataset_query import SqlRejected
from app.application.services.semantic_metrics import (
    NOT_SUPPORTED,
    build_query,
    queryable_metrics,
)
from app.domain.semantic import (
    ColumnSpec,
    DatasetDescription,
    KeySpec,
    LinkSpec,
    SemanticModelError,
    TableSpec,
)
from app.infrastructure.semantic import SemanticDataset
from app.infrastructure.semantic.sqlite_source import read_description
from app.interfaces.mcp.knowbase_mcp import ALL_TOOLS, KNOWN_TOOLS, KnowledgeMcp

HERE = Path(__file__).parent
V2 = HERE / "fixtures" / "sh600009-financials-v2.dataset.bin"
V1 = HERE / "fixtures" / "sh600009-financials.dataset.bin"
V2_VERSION = "sh600009-financials-9fd91db79529e208"
ANNUAL = {"column": "report_type", "op": "eq", "value": "年报"}
QUERYABLE = {
    "current_ratio",
    "debt_ratio",
    "deduct_ratio",
    "free_cash_flow",
    "gross_margin",
    "interest_bearing_debt",
    "invest_income_share",
    "net_margin",
    "ocf_to_netprofit",
}


@pytest.fixture(scope="module")
def dataset(tmp_path_factory) -> SemanticDataset:
    target = SemanticDataset(
        V2,
        dataset_id="sh600009-financials",
        cache_dir=tmp_path_factory.mktemp("semantic-metrics"),
    )
    yield target
    target.close()


@pytest.fixture(scope="module")
def description() -> DatasetDescription:
    return read_description(V2)


def truth(sql: str) -> list[tuple]:
    with sqlite3.connect(f"file:{V2}?mode=ro", uri=True) as c:
        return c.execute(sql).fetchall()


def close(left, right) -> bool:
    if left is None or right is None:
        return left is right
    return abs(left - right) <= 1e-9 * max(1.0, abs(left))


# ---------------------------------------------------------------- 自述第二版


def test_the_description_carries_links_and_keys(description):
    assert description.metadata["data_snapshot_id"] == V2_VERSION
    assert description.metadata["dataset_export_version"] == "2.0.0"
    assert len(description.links) == 6
    assert description.link("cash_flow", "income_statement") == LinkSpec(
        "cash_flow_to_income_statement",
        "cash_flow",
        "income_statement",
        "one_to_one",
        ("security_code", "report_date"),
    )
    assert description.link("cash_flow", "official_key_figures") is None
    assert description.key("balance_sheet") == KeySpec(
        "balance_sheet",
        ("security_code", "report_date"),
        ("report_type", "fiscal_year", "basis", "verified"),
    )
    assert description.key("disclosure_calendar") is None


def test_the_first_version_has_neither(tmp_path):
    old = read_description(V1)
    assert old.links == () and old.keys == ()
    assert queryable_metrics(old) == {}


def test_queryable_metrics_are_those_with_a_valid_definition(description):
    metrics = queryable_metrics(description)
    assert set(metrics) == QUERYABLE
    ratio = metrics["ocf_to_netprofit"]
    assert ratio.base_table == "cash_flow"
    assert ratio.linked_tables == ("income_statement",)
    assert ratio.value_sql == (
        '"cash_flow"."netcash_operate" / "income_statement"."netprofit"'
    )
    assert ratio.applicable_sql == '"income_statement"."netprofit" > 1'
    assert ratio.formula == "netcash_operate / income_statement.netprofit"
    assert metrics["free_cash_flow"].applicable_sql is None
    assert metrics["gross_margin"].unit == "比率"


def simple(*rows: dict, links=(), keys=None) -> DatasetDescription:
    columns = (
        ColumnSpec("id", "BIGINT"),
        ColumnSpec("kind", "VARCHAR"),
        ColumnSpec("a", "DOUBLE"),
        ColumnSpec("b", "DOUBLE"),
    )
    other = (ColumnSpec("id", "BIGINT"), ColumnSpec("c", "DOUBLE"))
    base = {
        "kind": "row",
        "base_table": "facts",
        "queryable": 1,
        "display_name": "x",
        "unit": "",
    }
    return DatasetDescription(
        tables=(TableSpec("facts", columns, 0), TableSpec("other", other, 0)),
        metrics=tuple(base | row for row in rows),
        links=links,
        keys=(KeySpec("facts", ("id",), ("kind",)),) if keys is None else keys,
    )


@pytest.mark.parametrize(
    "expression",
    [
        "a / b",
        "(a - b) / NULLIF(b, 0)",
        "COALESCE(a, 0) + COALESCE(b, 0)",
        "CASE WHEN b > 0 THEN a / b ELSE NULL END",
        "ROUND(ABS(a) * 100, 2)",
        "-a + 1.5e3",
        "a > 1 AND NOT (b < 0 OR b IS NULL)",
        "facts.a / facts.b",
    ],
)
def test_expressions_made_of_columns_and_arithmetic_are_accepted(expression):
    found = queryable_metrics(
        simple({"metric_name": "m", "value_expression": expression})
    )
    assert set(found) == {"m"}


@pytest.mark.parametrize(
    "expression",
    [
        "(SELECT MAX(a) FROM facts)",
        "a / (SELECT 1)",
        "SUM(a)",
        "read_text('/etc/passwd')",
        "getenv('HOME')",
        "a; DROP TABLE facts",
        "nope / b",
        "other.c / b",  # 没有声明关系的表
        "ds.main.facts.a",
        "main.facts.a / b",
        "phys_facts.a",
        "LAG(a) OVER (ORDER BY id)",
        "a || 'x'",
        "CAST(a AS VARCHAR)",
        "random()",
        "a IN (SELECT a FROM facts)",
        "EXISTS (SELECT 1)",
        "a /",
        "",
    ],
)
def test_anything_else_makes_the_metric_unavailable(expression, caplog):
    description = simple(
        {"metric_name": "bad", "value_expression": expression},
        {"metric_name": "good", "value_expression": "a / b"},
    )
    assert set(queryable_metrics(description)) == {"good"}  # 别的口径不受影响
    if expression:
        assert "semantic_metric_rejected metric=bad" in caplog.text


@pytest.mark.parametrize(
    "row",
    [
        {"metric_name": "m", "value_expression": "a", "queryable": 0},
        {"metric_name": "m", "value_expression": "a", "kind": "aggregate"},
        {"metric_name": "m", "value_expression": "a", "base_table": "nothing"},
        {"metric_name": "m", "value_expression": "a", "base_table": "other"},
        {"metric_name": "m", "value_expression": None},
        {"metric_name": "bad name", "value_expression": "a"},
        {"metric_name": "kind", "value_expression": "a"},  # 与基础表的字段同名
        {"metric_name": "m__applicable", "value_expression": "a"},
        {"metric_name": "m", "value_expression": "a", "applicable_when": "SUM(a) > 0"},
        {"metric_name": None, "value_expression": "a"},
    ],
)
def test_definitions_that_break_the_conventions_are_not_queryable(row):
    assert queryable_metrics(simple(row)) == {}


def test_a_linked_table_may_be_used_once_the_link_is_declared():
    link = LinkSpec("facts_to_other", "facts", "other", "many_to_one", ("id",))
    description = simple(
        {"metric_name": "m", "value_expression": "a / other.c"}, links=(link,)
    )
    description.validate()
    metric = queryable_metrics(description)["m"]
    assert metric.linked_tables == ("other",)
    sql = build_query(description, {"m": metric}, metrics=["m"]).sql
    assert 'LEFT JOIN other ON "facts"."id" = "other"."id"' in sql


@pytest.mark.parametrize(
    ("links", "keys", "message"),
    [
        (
            (LinkSpec("l", "facts", "other", "many_to_many", ("id",)),),
            None,
            "link kind",
        ),
        ((LinkSpec("l", "facts", "facts", "one_to_one", ("id",)),), None, "itself"),
        ((LinkSpec("l", "facts", "other", "one_to_one", ("a",)),), None, "missing"),
        ((LinkSpec("l", "facts", "gone", "one_to_one", ("id",)),), None, "missing"),
        ((LinkSpec("l", "facts", "other", "one_to_one", ()),), None, "missing"),
        (
            (
                LinkSpec("l1", "facts", "other", "one_to_one", ("id",)),
                LinkSpec("l2", "facts", "other", "one_to_one", ("id",)),
            ),
            None,
            "two links",
        ),
        ((), (KeySpec("facts", ("nope",), ()),), "key refers to a missing"),
        ((), (KeySpec("facts", ("id",), ("id",)),), "twice"),
        ((), (KeySpec("facts", (), ("kind",)),), "key refers to a missing"),
        (
            (),
            (KeySpec("facts", ("id",), ()), KeySpec("facts", ("kind",), ())),
            "two keys",
        ),
    ],
)
def test_links_and_keys_must_refer_to_what_exists(links, keys, message):
    with pytest.raises(SemanticModelError, match=message):
        simple(links=links, keys=keys).validate()


# ---------------------------------------------------------------- 结果与手算一致


SINGLE = {
    "gross_margin": (
        "income_statement",
        "(operate_income - operate_cost) / operate_income",
        "operate_income > 1",
    ),
    "net_margin": (
        "income_statement",
        "netprofit / operate_income",
        "operate_income > 1",
    ),
    "deduct_ratio": (
        "income_statement",
        "deduct_parent_netprofit / parent_netprofit",
        "parent_netprofit > 1",
    ),
    "invest_income_share": (
        "income_statement",
        "invest_income / operate_profit",
        "operate_profit > 1",
    ),
    "debt_ratio": (
        "balance_sheet",
        "total_liabilities / total_assets",
        "total_assets > 1",
    ),
    "current_ratio": (
        "balance_sheet",
        "total_current_assets / total_current_liab",
        "total_current_liab > 1",
    ),
    "interest_bearing_debt": (
        "balance_sheet",
        "COALESCE(short_loan,0) + COALESCE(noncurrent_liab_1year,0) + "
        "COALESCE(long_loan,0) + COALESCE(bond_payable,0) + COALESCE(lease_liab,0)",
        "1=1",
    ),
    "free_cash_flow": ("cash_flow", "netcash_operate - construct_long_asset", "1=1"),
}


def everything(dataset, metric: str) -> dict[str, dict]:
    """一个口径在基础表每一行上的结果，按报告期末日取。"""
    found: dict[str, dict] = {}
    low = "0000-00-00"
    while True:
        result = dataset.query_metric(
            [metric], filters=[{"column": "report_date", "op": "gt", "value": low}]
        )
        for row in result["rows"]:
            found[row["report_date"]] = row["metrics"][metric]
        if not result["truncated"]:
            return found
        low = result["rows"][-1]["report_date"]


@pytest.mark.parametrize("metric", sorted(SINGLE))
def test_every_row_agrees_with_the_hand_written_formula(dataset, metric):
    table, value, condition = SINGLE[metric]
    expected = truth(
        f"SELECT report_date, CASE WHEN {condition} THEN 1 ELSE 0 END, "  # noqa: S608
        f"CASE WHEN {condition} THEN {value} END FROM {table}"
    )
    actual = everything(dataset, metric)
    assert len(actual) == len(expected) >= 103
    for report_date, applicable, number in expected:
        got = actual[report_date]
        assert got["applicable"] is bool(applicable), (metric, report_date)
        assert close(got["value"], number), (metric, report_date, got, number)
        if not applicable:
            assert got["value"] is None and "不适用" in got["reason"]
        elif number is None:
            assert got["reason"] == "所需数据缺失"
        else:
            assert got["reason"] is None


def test_the_cross_statement_metric_agrees_with_a_hand_written_join(dataset):
    expected = truth(
        "SELECT c.report_date, CASE WHEN i.netprofit > 1 THEN 1 ELSE 0 END, "
        "CASE WHEN i.netprofit > 1 THEN c.netcash_operate / i.netprofit END "
        "FROM cash_flow c LEFT JOIN income_statement i "
        "ON i.security_code = c.security_code AND i.report_date = c.report_date"
    )
    actual = everything(dataset, "ocf_to_netprofit")
    assert len(actual) == len(expected) == 103
    for report_date, applicable, number in expected:
        got = actual[report_date]
        assert got["applicable"] is bool(applicable), report_date
        assert close(got["value"], number), report_date


def test_years_with_losses_are_reported_as_not_applicable(dataset):
    result = dataset.query_metric(
        ["invest_income_share", "gross_margin"],
        filters=[ANNUAL, {"column": "fiscal_year", "op": "gte", "value": 2019}],
    )
    rows = {r["fiscal_year"]: r for r in result["rows"]}
    assert sorted(rows) == [2019, 2020, 2021, 2022, 2023, 2024, 2025]
    for year in (2020, 2021, 2022):
        share = rows[year]["metrics"]["invest_income_share"]
        assert share == {
            "value": None,
            "unit": "比率",
            "applicable": False,
            "reason": "营业利润为负或在一元以内时不适用",
        }
        assert rows[year]["metrics"]["gross_margin"]["value"] < 0  # 毛利率为负仍然适用
    assert rows[2025]["metrics"]["invest_income_share"]["value"] == pytest.approx(
        0.27252579730010856
    )
    # 每一行都带着口径，专家不用再去查这一期是什么口径
    assert rows[2021]["basis"] == "追溯调整后" and rows[2020]["basis"] == "原始披露"
    assert rows[2025] | {"metrics": None} == {
        "security_code": "600009",
        "report_date": "2025-12-31",
        "report_type": "年报",
        "fiscal_year": 2025,
        "basis": "原始披露",
        "verified": 1,
        "metrics": None,
    }


def test_the_result_says_how_each_metric_was_computed(dataset):
    result = dataset.query_metric(
        ["ocf_to_netprofit"],
        filters=[ANNUAL, {"column": "fiscal_year", "op": "eq", "value": 2025}],
    )
    assert result["definitions"] == [
        {
            "metric_name": "ocf_to_netprofit",
            "display_name": "经营现金流与净利润之比",
            "unit": "倍",
            "base_table": "cash_flow",
            "formula": "netcash_operate / income_statement.netprofit",
            "applicable_when": "income_statement.netprofit > 1",
            "description": result["definitions"][0]["description"],
        }
    ]
    assert "新租赁准则" in result["definitions"][0]["description"]
    assert result["row_count"] == 1 and result["truncated"] is False
    citation = result["citation"]
    assert citation["data_version"] == V2_VERSION
    assert citation["engine"] == "wrenai-0.15.0" and len(citation["query_digest"]) == 16
    # 给出的 SQL 就是实际执行的那一条，经普通的查询工具再跑一次结果相同
    again = dataset.run_sql(result["sql"])
    assert again["rows"][0]["ocf_to_netprofit"] == pytest.approx(2.481450071337846)
    assert again["rows"][0]["ocf_to_netprofit__applicable"] is True
    assert "phys_" not in json.dumps(result, ensure_ascii=False)


def test_filters_and_ordering(dataset):
    result = dataset.query_metric(
        ["debt_ratio"],
        filters=[
            ANNUAL,
            {"column": "fiscal_year", "op": "in", "value": [2023, 2024, 2025]},
            {"column": "basis", "op": "neq", "value": "未核实"},
        ],
        order_by=[{"by": "debt_ratio", "direction": "desc"}],
    )
    values = [r["metrics"]["debt_ratio"]["value"] for r in result["rows"]]
    assert len(values) == 3 and values == sorted(values, reverse=True)
    result = dataset.query_metric(
        ["debt_ratio"],
        filters=[{"column": "fiscal_year", "op": "eq", "value": 2025}],
        order_by=[{"by": "report_date", "direction": "desc"}],
    )
    assert [r["report_type"] for r in result["rows"]] == [
        "年报",
        "三季报",
        "中报",
        "一季报",
    ]
    result = dataset.query_metric(
        ["debt_ratio"],
        filters=[{"column": "fiscal_year", "op": "not_in", "value": [2025]}, ANNUAL],
        max_rows=3,
    )
    assert result["row_count"] == 3 and result["truncated"] is True
    dates = [r["report_date"] for r in result["rows"]]
    assert dates == sorted(dates)  # 不指定排序时按主键


@pytest.mark.parametrize(
    ("arguments", "message"),
    [
        ({"metrics": []}, "list of 1 to 10"),
        ({"metrics": "gross_margin"}, "list of 1 to 10"),
        ({"metrics": None}, "list of 1 to 10"),
        ({"metrics": ["gross_margin", "gross_margin"]}, "different metric names"),
        ({"metrics": ["gross_margin", 3]}, "list of 1 to 10"),
        ({"metrics": [f"m{i}" for i in range(11)]}, "list of 1 to 10"),
        ({"metrics": ["roe_avg"]}, "cannot be queried by name: roe_avg"),
        ({"metrics": ["no_such"]}, "cannot be queried by name: no_such"),
        ({"metrics": ["operate_income"]}, "cannot be queried by name"),
        ({"metrics": ["gross_margin", "debt_ratio"]}, "different tables"),
        ({"metrics": ["gross_margin"], "filters": "x"}, "filters is a list"),
        ({"metrics": ["gross_margin"], "filters": [ANNUAL] * 11}, "at most 10"),
        ({"metrics": ["gross_margin"], "filters": ["x"]}, "each filter is an object"),
        (
            {"metrics": ["gross_margin"], "filters": [ANNUAL | {"sql": "1=1"}]},
            "each filter is an object",
        ),
        (
            {
                "metrics": ["gross_margin"],
                "filters": [{"column": "operate_income", "op": "gt", "value": 1}],
            },
            "filters may use these columns only",
        ),
        (
            {
                "metrics": ["gross_margin"],
                "filters": [{"column": "fiscal_year", "op": "like", "value": 1}],
            },
            "filter op must be one of",
        ),
        (
            {
                "metrics": ["gross_margin"],
                "filters": [{"column": "fiscal_year", "op": "eq", "value": "2025"}],
            },
            "needs a number",
        ),
        (
            {
                "metrics": ["gross_margin"],
                "filters": [{"column": "fiscal_year", "op": "eq", "value": 2025.5}],
            },
            "whole number",
        ),
        (
            {
                "metrics": ["gross_margin"],
                "filters": [{"column": "fiscal_year", "op": "eq", "value": True}],
            },
            "needs a number",
        ),
        (
            {
                "metrics": ["gross_margin"],
                "filters": [{"column": "report_type", "op": "eq", "value": 1}],
            },
            "needs a text value",
        ),
        (
            {
                "metrics": ["gross_margin"],
                "filters": [{"column": "fiscal_year", "op": "in", "value": []}],
            },
            "list of 1 to 50",
        ),
        (
            {
                "metrics": ["gross_margin"],
                "filters": [{"column": "fiscal_year", "op": "in", "value": 2025}],
            },
            "list of 1 to 50",
        ),
        ({"metrics": ["gross_margin"], "order_by": []}, "order_by is a list"),
        (
            {"metrics": ["gross_margin"], "order_by": [{"by": "net_margin"}]},
            "order_by may use",
        ),
        (
            {"metrics": ["gross_margin"], "order_by": [{"by": "operate_income"}]},
            "order_by may use",
        ),
        (
            {
                "metrics": ["gross_margin"],
                "order_by": [{"by": "fiscal_year", "direction": "up"}],
            },
            "asc or desc",
        ),
        ({"metrics": ["gross_margin"], "max_rows": 0}, "max_rows"),
    ],
)
def test_requests_that_are_refused(dataset, arguments, message):
    metrics = arguments.pop("metrics")
    with pytest.raises(SqlRejected, match=message):
        dataset.query_metric(metrics, **arguments)


@pytest.mark.parametrize(
    "value",
    [
        "年报' OR '1'='1",
        "年报'; DROP TABLE income_statement; --",
        "年报\\' OR 1=1 --",
        '年报" OR "1"="1',
        "' UNION SELECT * FROM phys_income_statement --",
    ],
)
def test_filter_values_are_data_and_never_sql(dataset, value):
    result = dataset.query_metric(
        ["gross_margin"],
        filters=[{"column": "report_type", "op": "eq", "value": value}],
    )
    assert result["rows"] == [] and result["row_count"] == 0
    assert dataset.run_sql("SELECT COUNT(*) AS n FROM income_statement")["rows"] == [
        {"n": 110}
    ]


def test_a_dataset_without_the_second_description_says_so(tmp_path):
    old = SemanticDataset(V1, dataset_id="old", cache_dir=tmp_path)
    try:
        with pytest.raises(SqlRejected) as caught:
            old.query_metric(["gross_margin"])
        assert str(caught.value) == NOT_SUPPORTED
        definitions = old.metric_definitions()["metrics"]
        assert len(definitions) == 10
        assert all(m["queryable"] is False for m in definitions)
        assert old.run_sql("SELECT COUNT(*) AS n FROM cash_flow")["rows"] == [
            {"n": 103}
        ]
    finally:
        old.close()


def test_metric_definitions_say_which_metrics_can_be_queried(dataset):
    definitions = {m["metric_name"]: m for m in dataset.metric_definitions()["metrics"]}
    assert {n for n, m in definitions.items() if m["queryable"]} == QUERYABLE
    assert definitions["roe_avg"]["queryable"] is False
    assert definitions["roe_avg"]["value_expression"] is None
    assert definitions["gross_margin"]["applicable_when"] == "operate_income > 1"
    assert definitions["gross_margin"]["expression_hint"]  # 原有的七列还在


def test_the_fifteen_truth_queries_also_run_on_the_second_version(dataset):
    cases = json.loads(
        (HERE / "fixtures" / "semantic" / "fin_truth_queries.json").read_text("utf-8")
    )
    for case in cases:
        assert dataset.run_sql(case["sql"])["row_count"] >= 1, case["query_id"]


# ---------------------------------------------------------------- 工具


def server(default_dataset, tmp_path, *, semantic: bool, fixture: Path = V2):
    settings = settings_for(
        default_dataset,
        tmp_path,
        knowbase_semantic_engine_enabled=semantic,
        knowbase_semantic_cache_dir=str(tmp_path / "semantic"),
    )
    target = KnowledgeMcp(settings)
    entry = replace(
        registered(registration()),
        data_version=V2_VERSION,
        sha256="ac5cfd08" + "0" * 56,
    )
    target.catalog._registry = MemoryRegistry(entry)
    target.catalog._files = LocalFiles(tmp_path, content=fixture.read_bytes())
    return target


async def ask(target, method: str, params: dict, token: str = TOKEN):
    grant = target.tokens.resolve(f"Bearer {token}")
    reply = await target.handle(
        grant, {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
    )
    return reply.get("result") or reply


async def call(target, name: str, arguments: dict, token: str = TOKEN):
    result = await ask(
        target, "tools/call", {"name": name, "arguments": arguments}, token
    )
    if "error" in result:
        return True, None, result["error"]["message"]
    return (
        result["isError"],
        result.get("structuredContent"),
        result["content"][0]["text"],
    )


async def test_the_tool_exists_only_while_the_semantic_layer_is_on(
    default_dataset, tmp_path
):
    assert frozenset(ALL_TOOLS) | {"query_metric"} == KNOWN_TOOLS
    off = server(default_dataset, tmp_path, semantic=False)
    listed = (await ask(off, "tools/list", {}))["tools"]
    assert [t["name"] for t in listed] == list(ALL_TOOLS)
    error, _, text = await call(off, "query_metric", {"metrics": ["gross_margin"]})
    assert error and text == "unknown tool: query_metric"
    on = server(default_dataset, tmp_path, semantic=True)
    listed = {t["name"]: t for t in (await ask(on, "tools/list", {}))["tools"]}
    assert set(listed) == KNOWN_TOOLS
    schema = listed["query_metric"]["inputSchema"]
    assert schema["required"] == ["metrics"] and not schema["additionalProperties"]
    assert "not applicable" in listed["query_metric"]["description"]


async def test_metrics_are_queried_by_name_through_the_tool(default_dataset, tmp_path):
    target = server(default_dataset, tmp_path, semantic=True)
    error, data, text = await call(
        target,
        "query_metric",
        {
            "dataset": "sh600009-financials",
            "metrics": ["gross_margin", "invest_income_share"],
            "filters": [
                ANNUAL,
                {"column": "fiscal_year", "op": "in", "value": [2020, 2025]},
            ],
            "order_by": [{"by": "fiscal_year", "direction": "asc"}],
        },
    )
    assert not error, text
    assert [r["fiscal_year"] for r in data["rows"]] == [2020, 2025]
    assert data["rows"][0]["metrics"]["invest_income_share"]["applicable"] is False
    assert data["rows"][1]["metrics"]["gross_margin"]["value"] == pytest.approx(
        0.27514317245874353
    )
    assert data["citation"]["dataset_id"] == "sh600009-financials"
    assert data["citation"]["data_version"] == V2_VERSION
    assert json.loads(text) == data


async def test_refusals_come_back_as_tool_errors(default_dataset, tmp_path):
    target = server(default_dataset, tmp_path, semantic=True)
    error, _, text = await call(
        target,
        "query_metric",
        {"dataset": "sh600009-financials", "metrics": ["roe_avg"]},
    )
    assert error and "cannot be queried by name: roe_avg" in text
    # 默认数据集（零售库的替身）没有第二版自述
    error, _, text = await call(
        target, "query_metric", {"metrics": ["net_revenue_cents"]}
    )
    assert error and text == NOT_SUPPORTED
    error, _, text = await call(
        target, "query_metric", {"dataset": "sh600519-financials", "metrics": ["x"]}
    )
    assert error and "unknown dataset" in text


async def test_a_token_limited_to_other_tools_cannot_use_it(default_dataset, tmp_path):
    target = server(default_dataset, tmp_path, semantic=True)
    error, _, text = await call(
        target,
        "query_metric",
        {"dataset": "sh600009-financials", "metrics": ["gross_margin"]},
        token=TOKEN_NO_LIST,
    )
    assert error and "not allowed for this token" in text
    listed = (await ask(target, "tools/list", {}, TOKEN_NO_LIST))["tools"]
    assert {t["name"] for t in listed} == {"describe_schema", "run_sql"}
