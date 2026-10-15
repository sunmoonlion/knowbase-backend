"""语义层（0009-semantic 甲段）：自述、语义模型、转换、执行、整条查询的路。

用的是 knowfeed 实建的 600009 数据集原件。零售库不进 git，在的时候才跑它的那部分。
不访问网络，不需要数据库服务。
"""

from __future__ import annotations

import json
import sqlite3
import threading
from decimal import Decimal
from pathlib import Path

import duckdb
import pytest

from app.application.services.dataset_query import SqlRejected
from app.application.services.semantic_model import build_manifest
from app.domain.semantic import (
    ColumnSpec,
    DatasetDescription,
    SemanticModelError,
    TableSpec,
)
from app.infrastructure.datasets import SqliteDatasetQueries
from app.infrastructure.external.dataset_store import DatasetUnavailable
from app.infrastructure.semantic import SemanticDataset
from app.infrastructure.semantic.duckdb_store import (
    DuckDbExecutor,
    convert,
    normalize,
    sanitize,
)
from app.infrastructure.semantic.sqlite_source import column_type, read_description

HERE = Path(__file__).parent
FINANCIAL = HERE / "fixtures" / "sh600009-financials.dataset.bin"
RETAIL = HERE.parent / "datasets" / "lesson23_business_analysis.sqlite"
VERSION = "sh600009-financials-39a395bfa6f16b67"
TABLES = {
    "balance_sheet": 110,
    "cash_flow": 103,
    "dataset_metadata": 17,
    "disclosure_calendar": 9,
    "field_dictionary": 84,
    "income_statement": 110,
    "metric_dictionary": 10,
    "official_key_figures": 177,
    "reconciliation_rules": 9,
}


def queries(name: str) -> list[dict[str, str]]:
    return json.loads((HERE / "fixtures" / "semantic" / name).read_text("utf-8"))


def same(left, right) -> bool:
    if isinstance(left, float) or isinstance(right, float):
        if left is None or right is None:
            return left is right
        return abs(float(left) - float(right)) <= 1e-9 * max(1.0, abs(float(left)))
    return left == right


@pytest.fixture(scope="module")
def cache(tmp_path_factory) -> Path:
    return tmp_path_factory.mktemp("semantic-cache")


@pytest.fixture(scope="module")
def financial(cache) -> SemanticDataset:
    dataset = SemanticDataset(
        FINANCIAL, dataset_id="sh600009-financials", cache_dir=cache
    )
    yield dataset
    dataset.close()


def small(tmp_path: Path, script: str) -> Path:
    path = tmp_path / "small.db"
    with sqlite3.connect(path) as c:
        c.executescript(script)
    return path


# ---------------------------------------------------------------- 自述


def test_the_description_lists_every_table_with_its_dictionary_entries():
    description = read_description(FINANCIAL)
    assert {t.name: t.row_count for t in description.tables} == TABLES
    income = description.table("income_statement")
    assert income.column("operate_income") == ColumnSpec(
        "operate_income", "DOUBLE", "营业收入", "元"
    )
    assert income.column("fiscal_year").type == "BIGINT"
    assert income.column("report_date") == ColumnSpec(
        "report_date", "VARCHAR", "报告期末日", None
    )
    assert income.column("security_code").type == "VARCHAR"
    assert description.metadata["data_snapshot_id"] == VERSION
    assert len(description.metrics) == 10


@pytest.mark.parametrize(
    ("declared", "expected"),
    [
        ("INTEGER", "BIGINT"),
        ("int", "BIGINT"),
        ("BIGINT", "BIGINT"),
        ("TEXT", "VARCHAR"),
        ("VARCHAR(20)", "VARCHAR"),
        ("REAL", "DOUBLE"),
        ("DOUBLE PRECISION", "DOUBLE"),
        ("FLOAT", "DOUBLE"),
    ],
)
def test_declared_types_map_to_three_engine_types(declared, expected):
    assert column_type("t", "c", declared) == expected


@pytest.mark.parametrize("declared", ["", "BLOB", "NUMERIC", "DECIMAL(10,2)", "DATE"])
def test_types_that_cannot_be_carried_over_faithfully_are_not_guessed(declared):
    with pytest.raises(SemanticModelError, match="unsupported column type: t.c"):
        column_type("t", "c", declared)


@pytest.mark.parametrize(
    ("script", "message"),
    [
        ("CREATE TABLE phys_orders(id INTEGER);", "reserved prefix"),
        ('CREATE TABLE "order lines"(id INTEGER);', "not an identifier"),
        ('CREATE TABLE t("bad name" INTEGER);', "column name is not an identifier"),
        ("CREATE TABLE t(v BLOB);", "unsupported column type"),
        ("CREATE TABLE t(v);", "unsupported column type"),
        ("CREATE VIEW v AS SELECT 1 AS n;", "no tables"),
    ],
)
def test_datasets_that_break_the_conventions_are_refused(tmp_path, script, message):
    with pytest.raises(SemanticModelError, match=message):
        read_description(small(tmp_path, script))


def test_a_missing_file_is_reported_as_missing(tmp_path):
    with pytest.raises(FileNotFoundError):
        read_description(tmp_path / "nothing.sqlite")


# ---------------------------------------------------------------- 语义模型


def test_models_carry_the_table_name_and_point_at_the_prefixed_table():
    manifest = build_manifest(read_description(FINANCIAL))
    assert [m["name"] for m in manifest["models"]] == sorted(TABLES)
    for model in manifest["models"]:
        assert model["tableReference"] == {
            "catalog": "ds",
            "schema": "main",
            "table": "phys_" + model["name"],
        }
        assert model["name"] != model["tableReference"]["table"]
    income = next(m for m in manifest["models"] if m["name"] == "income_statement")
    column = next(c for c in income["columns"] if c["name"] == "operate_income")
    assert column == {
        "name": "operate_income",
        "type": "DOUBLE",
        "isCalculated": False,
        "notNull": False,
        "properties": {"displayName": "营业收入", "description": "营业收入（单位 元）"},
    }
    assert manifest["dataSource"] == "duckdb" and manifest["cubes"] == []


def test_the_same_dataset_gives_the_same_model():
    first = build_manifest(read_description(FINANCIAL))
    second = build_manifest(read_description(FINANCIAL))
    assert json.dumps(first, sort_keys=True) == json.dumps(second, sort_keys=True)


def test_the_order_of_tables_in_the_file_does_not_matter():
    a = TableSpec("a", (ColumnSpec("x", "BIGINT"),), 0)
    b = TableSpec("b", (ColumnSpec("y", "VARCHAR"),), 0)
    assert build_manifest(DatasetDescription((a, b))) == build_manifest(
        DatasetDescription((b, a))
    )


def test_names_that_differ_only_by_case_are_refused():
    a = TableSpec("Orders", (ColumnSpec("x", "BIGINT"),), 0)
    b = TableSpec("orders", (ColumnSpec("x", "BIGINT"),), 0)
    with pytest.raises(SemanticModelError, match="differ only by case"):
        build_manifest(DatasetDescription((a, b)))
    c = TableSpec("t", (ColumnSpec("x", "BIGINT"), ColumnSpec("X", "BIGINT")), 0)
    with pytest.raises(SemanticModelError, match="differ only by case"):
        build_manifest(DatasetDescription((c,)))


# ---------------------------------------------------------------- 转换


def contents(database: Path) -> dict[str, list[tuple]]:
    db = duckdb.connect(str(database), read_only=True)
    try:
        names = [
            r[0]
            for r in db.execute(
                "SELECT table_name FROM information_schema.tables ORDER BY 1"
            ).fetchall()
        ]
        return {
            n: db.execute(f'SELECT * FROM "{n}" ORDER BY ALL').fetchall() for n in names
        }
    finally:
        db.close()


def test_every_row_and_value_is_carried_over(tmp_path):
    description = read_description(FINANCIAL)
    database = convert(FINANCIAL, description, tmp_path / "out.duckdb")
    converted = contents(database)
    assert set(converted) == {"phys_" + name for name in TABLES}
    with sqlite3.connect(f"file:{FINANCIAL}?mode=ro", uri=True) as src:
        for name, count in TABLES.items():
            rows = converted["phys_" + name]
            assert len(rows) == count
            columns = [c.name for c in description.table(name).columns]
            picked = ", ".join(f'"{c}"' for c in columns)
            original = src.execute(f'SELECT {picked} FROM "{name}"').fetchall()
            key = lambda row: tuple((v is None, str(v)) for v in row)  # noqa: E731
            for a, b in zip(
                sorted(original, key=key), sorted(rows, key=key), strict=True
            ):
                assert a == b, name
    assert list(tmp_path.glob("*.partial*")) == []


def test_converting_twice_gives_the_same_contents(tmp_path):
    description = read_description(FINANCIAL)
    first = contents(convert(FINANCIAL, description, tmp_path / "a.duckdb"))
    second = contents(convert(FINANCIAL, description, tmp_path / "b.duckdb"))
    assert first == second


def test_values_that_do_not_match_the_declared_type_stop_the_conversion(tmp_path):
    source = small(
        tmp_path,
        "CREATE TABLE t(n INTEGER); INSERT INTO t VALUES (1), ('not a number');",
    )
    with pytest.raises(SemanticModelError, match="declared types: t"):
        convert(source, read_description(source), tmp_path / "out.duckdb")
    assert list(tmp_path.glob("*.duckdb*")) == []


# ---------------------------------------------------------------- 执行


@pytest.fixture
def executor(tmp_path):
    source = small(
        tmp_path,
        "CREATE TABLE t(n INTEGER, v REAL, s TEXT);"
        "INSERT INTO t VALUES (1, 1.5, 'a'), (2, NULL, 'b'), (3, 2.5, NULL);",
    )
    database = convert(source, read_description(source), tmp_path / "x.duckdb")
    (tmp_path / "secret.txt").write_text("top secret")
    target = DuckDbExecutor(database)
    yield target, tmp_path
    target.close()


def run(executor, sql, limit=200, timeout_ms=5000):
    return executor.execute(sql, limit=limit, timeout_ms=timeout_ms)


def test_rows_come_back_in_the_order_the_query_asked_for(executor):
    target, _ = executor
    result = run(target, "SELECT n, v, s FROM ds.main.phys_t ORDER BY n DESC")
    assert result.columns == ["n", "v", "s"]
    assert result.rows == [
        {"n": 3, "v": 2.5, "s": None},
        {"n": 2, "v": None, "s": "b"},
        {"n": 1, "v": 1.5, "s": "a"},
    ]
    assert result.truncated is False


def test_rows_beyond_the_limit_are_cut_and_the_cut_is_reported(executor):
    target, _ = executor
    result = run(target, "SELECT n FROM ds.main.phys_t ORDER BY n", limit=2)
    assert [r["n"] for r in result.rows] == [1, 2] and result.truncated is True
    exact = run(target, "SELECT n FROM ds.main.phys_t ORDER BY n", limit=3)
    assert len(exact.rows) == 3 and exact.truncated is False


@pytest.mark.parametrize(
    "sql",
    [
        "INSERT INTO ds.main.phys_t VALUES (9, 9.0, 'z')",
        "DELETE FROM ds.main.phys_t",
        "DROP TABLE ds.main.phys_t",
        "CREATE TABLE ds.main.other AS SELECT 1 AS n",
        "SELECT * FROM read_text('{dir}/secret.txt')",
        "SELECT * FROM read_csv('{dir}/secret.txt')",
        "SELECT * FROM '{dir}/secret.txt'",
        "COPY (SELECT 1) TO '{dir}/out.csv'",
        "ATTACH DATABASE '{dir}/other.duckdb' AS other",
        "INSTALL httpfs",
        "SET enable_external_access=true",
        "SET lock_configuration=false",
        "EXPORT DATABASE '{dir}/dump'",
    ],
)
def test_the_database_itself_refuses_writes_and_the_outside_world(executor, sql):
    """前置检查与引擎之外的最后一层：就算前两层都漏了，库也不做这些事。"""
    target, directory = executor
    with pytest.raises(SqlRejected):
        run(target, sql.replace("{dir}", str(directory)))
    assert not (directory / "out.csv").exists()
    assert not (directory / "other.duckdb").exists()
    assert not (directory / "dump").exists()
    assert run(target, "SELECT COUNT(*) AS n FROM ds.main.phys_t").rows == [{"n": 3}]


def test_a_query_that_runs_too_long_is_interrupted(executor):
    target, _ = executor
    slow = (
        "SELECT COUNT(*) AS n FROM range(100000000) a, range(100000000) b "
        "WHERE a.range + b.range < 0"
    )
    with pytest.raises(SqlRejected, match="exceeded 200 ms and was cancelled"):
        run(target, slow, timeout_ms=200)
    assert run(target, "SELECT 1 AS n").rows == [{"n": 1}]  # 之后照常可用


def test_queries_from_several_threads_do_not_disturb_each_other(executor):
    target, _ = executor
    results: list[int] = []

    def work(n: int) -> None:
        for _ in range(20):
            rows = run(target, f"SELECT {n} AS n FROM ds.main.phys_t LIMIT 1").rows
            results.append(rows[0]["n"] - n)

    threads = [threading.Thread(target=work, args=(i,)) for i in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert results == [0] * 120


def test_duplicate_column_names_are_refused_instead_of_silently_merged(executor):
    target, _ = executor
    with pytest.raises(SqlRejected, match="duplicate column names"):
        run(target, "SELECT n, n FROM ds.main.phys_t")


def test_values_are_reduced_to_plain_types(executor):
    target, _ = executor
    row = run(
        target,
        "SELECT CAST(12 AS DECIMAL(18,2)) AS whole,"
        " CAST(1.25 AS DECIMAL(18,2)) AS part,"
        " DATE '2025-12-31' AS d, TIMESTAMP '2025-12-31 08:00:00' AS ts,"
        " CAST('NaN' AS DOUBLE) AS nan, CAST('Infinity' AS DOUBLE) AS inf,"
        " 170141183460469231731687303715884105727 AS huge, TRUE AS flag,"
        " [1, 2] AS items, NULL AS nothing, 1/2 AS half",
    ).rows[0]
    assert row == {
        "whole": 12,
        "part": 1.25,
        "d": "2025-12-31",
        "ts": "2025-12-31T08:00:00",
        "nan": None,
        "inf": None,
        "huge": 170141183460469231731687303715884105727,
        "flag": True,
        "items": [1, 2],
        "nothing": None,
        "half": 0.5,
    }
    json.dumps(row)


def test_normalize_handles_each_kind():
    assert normalize(Decimal("3.0")) == 3 and isinstance(normalize(Decimal("3.0")), int)
    assert normalize(Decimal("NaN")) is None
    assert normalize({"a": Decimal("1.5")}) == {"a": 1.5}
    with pytest.raises(SqlRejected, match="binary"):
        normalize(b"\x00")


@pytest.mark.parametrize(
    ("message", "expected"),
    [
        (
            "Catalog Error: Table with name income_statement does not exist!\n"
            'Did you mean "phys_income_statement"?',
            "Catalog Error: Table with name income_statement does not exist!",
        ),
        (
            'Binder Error: Referenced column "nope" not found in FROM clause!\n'
            'Candidate bindings: "1"\nLINE 1: ...FROM ds.main.phys_t AS __source',
            'Binder Error: Referenced column "nope" not found in FROM clause!',
        ),
        (
            'IO Error: Cannot open file "/tmp/knowledge-semantic/abc-b1.duckdb"',
            'IO Error: Cannot open file "<path>"',
        ),
        ("Error in ds.main.phys_cash_flow near x", "Error in cash_flow near x"),
        ('Error in "ds"."main"."phys_cash_flow"', 'Error in "ds"."main".cash_flow'),
        ("x" * 1000, "x" * 300),
    ],
)
def test_error_messages_lose_internal_names_paths_and_sql_excerpts(message, expected):
    assert sanitize(message) == expected


# ---------------------------------------------------------------- 整条查询的路


def test_the_dataset_describes_itself(financial):
    info = financial.info()
    assert (info.dataset_id, info.data_version) == ("sh600009-financials", VERSION)
    assert (info.start_date, info.end_date) == ("1994-12-31", "2026-06-30")
    schema = financial.describe_schema()
    assert {t["name"]: t["row_count"] for t in schema["tables"]} == TABLES
    assert schema["sql_dialect"] == "duckdb"
    assert "phys_" not in json.dumps(schema)
    one = financial.describe_schema("cash_flow")["tables"]
    assert [t["name"] for t in one] == ["cash_flow"]
    column = next(c for c in one[0]["columns"] if c["name"] == "netcash_operate")
    assert column == {
        "name": "netcash_operate",
        "type": "DOUBLE",
        "display_name": "经营活动产生的现金流量净额",
        "unit": "元",
    }
    with pytest.raises(SqlRejected, match="unknown table: phys_cash_flow"):
        financial.describe_schema("phys_cash_flow")


def test_metric_definitions_are_those_of_the_dataset(financial):
    old = SqliteDatasetQueries(FINANCIAL, dataset_id="sh600009-financials")
    new = financial.metric_definitions()["metrics"]
    # 语义层多给一项「能不能按名查询」；这份是第一版自述，都不能
    assert [m.pop("queryable") for m in new] == [False] * 10
    assert new == old.metric_definitions()["metrics"]
    found = financial.metric_definitions("毛利率")["metrics"]
    assert [m["metric_name"] for m in found] == ["gross_margin"]
    assert financial.metric_definitions("no such metric")["metrics"] == []


def test_results_carry_the_same_citation_plus_the_engine(financial):
    result = financial.run_sql("SELECT COUNT(*) AS n FROM cash_flow")
    assert result["rows"] == [{"n": 103}] and result["row_count"] == 1
    citation = result["citation"]
    assert citation["dataset_id"] == "sh600009-financials"
    assert citation["data_version"] == VERSION and citation["as_of"] == "2026-06-30"
    assert citation["engine"] == "wrenai-0.15.0"
    assert len(citation["query_digest"]) == 16
    again = financial.run_sql(" SELECT COUNT(*) AS n FROM cash_flow; ")
    assert again["citation"]["query_digest"] == citation["query_digest"]


def compare(dataset, source: Path, cases: list[dict[str, str]]) -> None:
    old = SqliteDatasetQueries(source, dataset_id=dataset.dataset_id)
    for case in cases:
        label = f"{case['case_id']}/{case['query_id']}"
        expected = old.run_sql(case["sql"])
        actual = dataset.run_sql(case["sql"])
        assert actual["columns"] == expected["columns"], label
        assert actual["truncated"] == expected["truncated"], label
        assert len(actual["rows"]) == len(expected["rows"]), label
        for a, b in zip(actual["rows"], expected["rows"], strict=True):
            assert all(same(a[k], b[k]) for k in b), (label, a, b)


def test_the_fifteen_financial_truth_queries_give_the_same_results(financial):
    cases = queries("fin_truth_queries.json")
    assert len(cases) == 15
    compare(financial, FINANCIAL, cases)


@pytest.mark.skipif(not RETAIL.is_file(), reason="the retail dataset is not in git")
def test_the_thirty_one_retail_truth_queries_give_the_same_results(cache):
    cases = queries("retail_truth_queries.json")
    assert len(cases) == 31
    dataset = SemanticDataset(RETAIL, dataset_id="retail", cache_dir=cache)
    try:
        compare(dataset, RETAIL, cases)
    finally:
        dataset.close()


ATTACKS = [
    "DELETE FROM income_statement",
    "INSERT INTO income_statement (fiscal_year) VALUES (1)",
    "CREATE TABLE evil AS SELECT 1",
    "SELECT 1; DROP TABLE income_statement",
    "ATTACH DATABASE '/tmp/x.db' AS x",
    "PRAGMA database_list",
    "COPY (SELECT 1) TO '/tmp/semantic-test-out.csv'",
    "INSTALL httpfs",
    "SELECT * FROM read_text('/etc/hostname')",
    "SELECT * FROM read_csv('/etc/passwd', delim=':', header=false)",
    "SELECT * FROM '/etc/passwd'",
    "SELECT * FROM duckdb_secrets()",
    "SELECT getenv('HOME') AS home",
    "SELECT table_name FROM information_schema.tables",
    "SELECT * FROM duckdb_tables()",
    "SELECT COUNT(*) AS n FROM phys_income_statement",
    "SELECT COUNT(*) AS n FROM ds.main.phys_income_statement",
    "SELECT COUNT(*) AS n FROM ds.main.income_statement",
    "SELECT COUNT(*) AS n FROM main.income_statement",
    'SELECT COUNT(*) AS n FROM "ds"."main"."phys_income_statement"',
    "WITH c AS (SELECT * FROM ds.main.phys_income_statement) SELECT COUNT(*) FROM c",
    "SELECT (SELECT COUNT(*) FROM ds.main.phys_cash_flow) AS n FROM income_statement",
    "SELECT * FROM sqlite_master",
    "SELECT * FROM no_such_table",
]


@pytest.mark.parametrize("sql", ATTACKS)
def test_attacks_are_refused_and_the_refusal_names_nothing_internal(financial, sql):
    with pytest.raises(SqlRejected) as caught:
        financial.run_sql(sql)
    message = str(caught.value)
    if "phys_" not in sql.lower():
        assert "phys_" not in message.lower()
    assert "/tmp" not in message and ".duckdb" not in message
    assert len(message) <= 400
    assert not Path("/tmp/semantic-test-out.csv").exists()  # noqa: S108


@pytest.mark.parametrize(
    ("sql", "message"),
    [
        ("SELECT nope FROM income_statement", 'column "nope" not found'),
        ("SELECT fiscal_year FROM income_statement WHERE", "could not be parsed"),
        ("SELECT fiscal_year + 'x' FROM income_statement", "query failed"),
    ],
)
def test_mistakes_get_a_useful_answer_without_planned_sql(financial, sql, message):
    with pytest.raises(SqlRejected) as caught:
        financial.run_sql(sql)
    text = str(caught.value)
    assert message in text
    for internal in ("phys_", "__source", "wren_src", "LINE 1", "Candidate"):
        assert internal not in text


@pytest.mark.parametrize("max_rows", [0, -1, "10", 1.5, True])
def test_max_rows_must_be_a_positive_integer(financial, max_rows):
    with pytest.raises(SqlRejected, match="max_rows"):
        financial.run_sql("SELECT 1 AS n", max_rows=max_rows)


def test_max_rows_is_capped(financial):
    sql = "SELECT report_date FROM income_statement ORDER BY report_date"
    result = financial.run_sql(sql, max_rows=100000)
    assert result["row_count"] == 110 and result["truncated"] is False
    result = financial.run_sql(sql, max_rows=5)
    assert result["row_count"] == 5 and result["truncated"] is True


# ---------------------------------------------------------------- 缓存


def test_the_converted_database_is_kept_and_reused(tmp_path):
    first = SemanticDataset(FINANCIAL, dataset_id="d", cache_dir=tmp_path)
    assert first.run_sql("SELECT COUNT(*) AS n FROM cash_flow")["rows"] == [{"n": 103}]
    first.close()
    (database,) = tmp_path.glob("*.duckdb")
    assert database.name == (
        "51900174f4b5b9a1d6f7" + database.name[20:64] + "-b1.duckdb"
    )
    stamp = database.stat().st_mtime_ns
    second = SemanticDataset(FINANCIAL, dataset_id="d", cache_dir=tmp_path)
    assert second.run_sql("SELECT COUNT(*) AS n FROM cash_flow")["rows"] == [{"n": 103}]
    second.close()
    assert database.stat().st_mtime_ns == stamp
    assert len(list(tmp_path.glob("*.duckdb"))) == 1


def test_a_damaged_cache_is_rebuilt(tmp_path):
    first = SemanticDataset(FINANCIAL, dataset_id="d", cache_dir=tmp_path)
    first.info()
    first.close()
    (database,) = tmp_path.glob("*.duckdb")
    db = duckdb.connect(str(database))
    db.execute("DELETE FROM phys_cash_flow WHERE fiscal_year = 2025")
    db.close()
    second = SemanticDataset(FINANCIAL, dataset_id="d", cache_dir=tmp_path)
    assert second.run_sql("SELECT COUNT(*) AS n FROM cash_flow")["rows"] == [{"n": 103}]
    second.close()
    database.write_bytes(b"not a database")
    third = SemanticDataset(FINANCIAL, dataset_id="d", cache_dir=tmp_path)
    assert third.run_sql("SELECT COUNT(*) AS n FROM cash_flow")["rows"] == [{"n": 103}]
    third.close()


def test_the_source_file_is_never_written(tmp_path):
    before = FINANCIAL.read_bytes()
    dataset = SemanticDataset(FINANCIAL, dataset_id="d", cache_dir=tmp_path)
    dataset.run_sql("SELECT COUNT(*) AS n FROM cash_flow")
    dataset.close()
    assert FINANCIAL.read_bytes() == before


def test_a_dataset_that_breaks_the_conventions_is_unavailable(tmp_path, caplog):
    source = small(tmp_path, "CREATE TABLE phys_orders(id INTEGER);")
    dataset = SemanticDataset(source, dataset_id="bad", cache_dir=tmp_path / "c")
    with pytest.raises(DatasetUnavailable) as caught:
        dataset.info()
    assert "phys_orders" not in str(caught.value)
    assert "reserved prefix" in caplog.text  # 原因进日志


def test_the_default_dataset_is_fetched_before_it_is_opened(tmp_path):
    calls: list[str] = []
    dataset = SemanticDataset(
        FINANCIAL,
        dataset_id="d",
        cache_dir=tmp_path,
        ensure=lambda: calls.append("fetched"),
    )
    assert calls == []
    dataset.info()
    dataset.info()
    dataset.close()
    assert calls == ["fetched"]
