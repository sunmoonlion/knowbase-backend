"""样例数据集 olist-ecommerce 的生成。

设计：k8s 仓 tree-build/SDD/modules/0023-fin-forecast.md 第三节。
用几行合成数据现造 parquet，不联网；定义文件用仓里那一份，
只把 sha256 换成合成文件的。
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import duckdb
import pytest
import yaml
from test_knowledge_datasets import (
    BUCKET,
    TOKEN,
    LocalFiles,
    MemoryRegistry,
    registered,
    registration,
    settings_for,
)
from test_knowledge_datasets import default_dataset as default_dataset

from app.infrastructure.semantic.sqlite_source import read_description
from app.interfaces.mcp.knowledge_mcp import KnowledgeMcp
from scripts.build_olist_dataset import (
    DECLARATION,
    BuildError,
    build,
    load_declaration,
    main,
    sha256_of,
)

BUSINESS = {
    "orders",
    "order_items",
    "order_payments",
    "order_reviews",
    "products",
    "customers",
    "sellers",
    "geolocation",
    "product_category_translation",
}
SELF_DESCRIPTION = {
    "dataset_metadata",
    "field_dictionary",
    "metric_dictionary",
    "table_links",
    "table_keys",
}

# 每张表一条 SELECT，列的顺序与类型和真实 parquet 一样。
# o1 已送达、o2 已取消、o3 已送达、o4 不可用且没有明细；
# c1 与 c2 是同一个真实客户（u1）；r1 一个评价编号对两单。
ROWS = {
    "olist_orders_dataset": """
        SELECT order_id, customer_id, order_status,
               a::TIMESTAMP AS order_purchase_timestamp,
               b::TIMESTAMP AS order_approved_at,
               c::TIMESTAMP AS order_delivered_carrier_date,
               d::TIMESTAMP AS order_delivered_customer_date,
               e::TIMESTAMP AS order_estimated_delivery_date
        FROM (VALUES
          ('o1', 'c1', 'delivered', '2017-01-05 10:00:00',
           '2017-01-05 10:30:00', '2017-01-06 08:00:00',
           '2017-01-10 15:00:00', '2017-01-20 00:00:00'),
          ('o2', 'c2', 'canceled', '2017-01-20 09:00:00',
           NULL, NULL, NULL, '2017-02-05 00:00:00'),
          ('o3', 'c3', 'delivered', '2017-02-03 11:00:00',
           '2017-02-03 11:10:00', '2017-02-04 09:00:00',
           '2017-02-09 12:00:00', '2017-02-20 00:00:00'),
          ('o4', 'c4', 'unavailable', '2017-02-10 12:00:00',
           NULL, NULL, NULL, '2017-03-01 00:00:00')
        ) AS t(order_id, customer_id, order_status, a, b, c, d, e)
    """,
    "olist_order_items_dataset": """
        SELECT order_id, n::BIGINT AS order_item_id, product_id, seller_id,
               s::TIMESTAMP AS shipping_limit_date,
               p::DOUBLE AS price, f::DOUBLE AS freight_value
        FROM (VALUES
          ('o1', 1, 'p1', 's1', '2017-01-07 00:00:00', 10.0, 2.0),
          ('o1', 2, 'p2', 's1', '2017-01-07 00:00:00', 5.0, 1.0),
          ('o2', 1, 'p1', 's1', '2017-01-22 00:00:00', 7.0, 1.0),
          ('o3', 1, 'p2', 's1', '2017-02-05 00:00:00', 20.0, 3.0)
        ) AS t(order_id, n, product_id, seller_id, s, p, f)
    """,
    "olist_order_payments_dataset": """
        SELECT order_id, n::BIGINT AS payment_sequential, payment_type,
               k::BIGINT AS payment_installments, v::DOUBLE AS payment_value
        FROM (VALUES
          ('o1', 1, 'credit_card', 3, 18.0),
          ('o2', 1, 'voucher', 1, 8.0),
          ('o3', 1, 'boleto', 1, 23.0)
        ) AS t(order_id, n, payment_type, k, v)
    """,
    "olist_products_dataset": """
        SELECT product_id, product_category_name,
               a::BIGINT AS product_name_lenght,
               b::BIGINT AS product_description_lenght,
               c::BIGINT AS product_photos_qty,
               d::BIGINT AS product_weight_g,
               e::BIGINT AS product_length_cm,
               f::BIGINT AS product_height_cm,
               g::BIGINT AS product_width_cm
        FROM (VALUES
          ('p1', 'beleza_saude', 40, 300, 2, 500, 20, 10, 15),
          ('p2', NULL, NULL, NULL, NULL, 800, 30, 20, 25)
        ) AS t(product_id, product_category_name, a, b, c, d, e, f, g)
    """,
    "olist_order_reviews_dataset": """
        SELECT review_id, order_id, n::BIGINT AS review_score,
               title AS review_comment_title,
               message AS review_comment_message,
               a::TIMESTAMP AS review_creation_date,
               b::TIMESTAMP AS review_answer_timestamp
        FROM (VALUES
          ('r1', 'o1', 5, NULL, 'otimo',
           '2017-01-11 00:00:00', '2017-01-12 08:00:00'),
          ('r1', 'o3', 4, NULL, NULL,
           '2017-02-10 00:00:00', '2017-02-11 08:00:00')
        ) AS t(review_id, order_id, n, title, message, a, b)
    """,
    "olist_customers_dataset": """
        SELECT a AS customer_id, b AS customer_unique_id,
               c AS customer_zip_code_prefix, d AS customer_city,
               e AS customer_state
        FROM (VALUES
          ('c1', 'u1', '01003', 'sao paulo', 'SP'),
          ('c2', 'u1', '01003', 'sao paulo', 'SP'),
          ('c3', 'u2', '20040', 'rio de janeiro', 'RJ'),
          ('c4', 'u3', '30110', 'belo horizonte', 'MG')
        ) AS t(a, b, c, d, e)
    """,
    "olist_sellers_dataset": """
        SELECT * FROM (VALUES ('s1', '13023', 'campinas', 'SP'))
        AS t(seller_id, seller_zip_code_prefix, seller_city, seller_state)
    """,
    "olist_geolocation_dataset": """
        SELECT z AS geolocation_zip_code_prefix,
               a::DOUBLE AS geolocation_lat, b::DOUBLE AS geolocation_lng,
               c AS geolocation_city, s AS geolocation_state
        FROM (VALUES
          ('01003', -23.55, -46.63, 'sao paulo', 'SP'),
          ('01003', -23.54, -46.64, 'são paulo', 'SP')
        ) AS t(z, a, b, c, s)
    """,
    "product_category_name_translation": """
        SELECT * FROM (VALUES ('beleza_saude', 'health_beauty'))
        AS t(product_category_name, product_category_name_english)
    """,
}

MONTHLY_GMV = """
SELECT substr(o.order_purchase_timestamp, 1, 7) AS period,
       SUM(i.price + i.freight_value) AS gmv,
       COUNT(DISTINCT i.order_id) AS order_count
FROM order_items i JOIN orders o ON o.order_id = i.order_id
WHERE o.order_status NOT IN ('canceled', 'unavailable')
GROUP BY 1 ORDER BY 1
"""


@pytest.fixture
def sample(tmp_path: Path) -> tuple[Path, Path]:
    """（合成的 parquet 目录，sha256 换成合成文件的定义文件）。"""
    cache = tmp_path / "parquet"
    cache.mkdir()
    duck = duckdb.connect(":memory:")
    for source, select in ROWS.items():
        target = str(cache / f"{source}.parquet")
        duck.execute(f"COPY ({select}) TO '{target}' (FORMAT parquet)")
    duck.close()
    declaration = load_declaration(DECLARATION)
    for table in declaration["tables"]:
        table["sha256"] = sha256_of(cache / f"{table['source']}.parquet")
    path = tmp_path / "olist.yml"
    path.write_text(yaml.safe_dump(declaration, allow_unicode=True), encoding="utf-8")
    return cache, path


def connect(path: Path) -> sqlite3.Connection:
    return sqlite3.connect(f"file:{path}?mode=ro", uri=True)


def test_the_declaration_matches_the_design():
    declaration = load_declaration(DECLARATION)
    assert {t["name"] for t in declaration["tables"]} == BUSINESS
    assert len(declaration["links"]) == 7
    assert len(declaration["metrics"]) == 11
    assert {m["kind"] for m in declaration["metrics"]} == {"aggregate", "unavailable"}
    assert "CC BY-NC-SA 4.0" in declaration["title"]
    # 两张主键在 WrenAI 原定义里写错的表，这里是两列
    keys = {t["name"]: t.get("key") for t in declaration["tables"]}
    assert keys["order_items"] == ["order_id", "order_item_id"]
    assert keys["order_payments"] == ["order_id", "payment_sequential"]
    assert keys["order_reviews"] == ["review_id", "order_id"]


def test_the_built_file_is_a_dataset_the_service_can_read(sample, tmp_path):
    cache, declaration = sample
    out = tmp_path / "out" / "olist.sqlite"
    result = build(declaration, cache, out)

    assert result["dataset_id"] == "olist-ecommerce"
    assert result["data_version"].startswith("olist-ecommerce-")
    assert result["sha256"] == sha256_of(out)
    assert result["size_bytes"] == out.stat().st_size
    assert (result["start_date"], result["end_date"]) == ("2017-01-05", "2017-02-10")
    assert not out.with_name(out.name + ".part").exists()

    description = read_description(out)
    assert {t.name for t in description.tables} == BUSINESS | SELF_DESCRIPTION
    assert description.metadata["data_snapshot_id"] == result["data_version"]
    assert description.metadata["complete_end"] == "2018-08"
    assert "geolocation" not in {
        name for link in description.links for name in (link.from_table, link.to_table)
    }
    assert len(description.links) == 7
    items = description.key("order_items")
    assert items is not None and items.key_columns == ("order_id", "order_item_id")
    assert description.key("geolocation") is None
    price = description.table("order_items").column("price")  # type: ignore[union-attr]
    assert (price.display_name, price.unit) == ("售价", "雷亚尔")  # type: ignore[union-attr]


def test_values_keep_their_shape(sample, tmp_path):
    cache, declaration = sample
    out = tmp_path / "olist.sqlite"
    build(declaration, cache, out)
    with connect(out) as db:
        types = {r[1]: r[2] for r in db.execute("PRAGMA table_info(orders)")}
        assert types["order_purchase_timestamp"] == "TEXT"
        assert db.execute(
            "SELECT order_purchase_timestamp, order_approved_at FROM orders "
            "WHERE order_id IN ('o1', 'o2') ORDER BY order_id"
        ).fetchall() == [
            ("2017-01-05 10:00:00", "2017-01-05 10:30:00"),
            ("2017-01-20 09:00:00", None),
        ]
        assert db.execute(
            "SELECT customer_zip_code_prefix FROM customers WHERE customer_id = 'c1'"
        ).fetchone() == ("01003",)
        assert db.execute(
            "SELECT product_category_name FROM products WHERE product_id = 'p2'"
        ).fetchone() == (None,)
        metrics = db.execute(
            "SELECT metric_name, kind, queryable, row_filter FROM metric_dictionary"
        ).fetchall()
    assert len(metrics) == 11
    assert all(queryable == 0 for _, _, queryable, _ in metrics)
    by_name = {name: (kind, row_filter) for name, kind, _, row_filter in metrics}
    assert by_name["gmv"] == (
        "aggregate",
        "orders.order_status NOT IN ('canceled', 'unavailable')",
    )
    assert by_name["gross_margin"][0] == "unavailable"


def test_the_same_input_gives_the_same_version_and_file(sample, tmp_path):
    cache, declaration = sample
    first = build(declaration, cache, tmp_path / "a.sqlite")
    second = build(declaration, cache, tmp_path / "b.sqlite")
    assert first["data_version"] == second["data_version"]
    assert first["sha256"] == second["sha256"]


def test_a_changed_declaration_changes_the_version(sample, tmp_path):
    cache, declaration = sample
    before = build(declaration, cache, tmp_path / "a.sqlite")["data_version"]
    edited = yaml.safe_load(declaration.read_text(encoding="utf-8"))
    edited["metadata"]["note_category"] += "。"
    declaration.write_text(yaml.safe_dump(edited, allow_unicode=True), encoding="utf-8")
    after = build(declaration, cache, tmp_path / "b.sqlite")["data_version"]
    assert before != after


def test_a_file_that_does_not_match_its_checksum_stops_the_build(
    sample, tmp_path, capsys
):
    cache, declaration = sample
    edited = yaml.safe_load(declaration.read_text(encoding="utf-8"))
    edited["tables"][0]["sha256"] = "0" * 64
    declaration.write_text(yaml.safe_dump(edited, allow_unicode=True), encoding="utf-8")
    out = tmp_path / "olist.sqlite"

    code = main(
        ["--cache", str(cache), "--out", str(out), "--declaration", str(declaration)]
    )

    assert code == 2
    assert "does not match its sha256" in capsys.readouterr().err
    assert not out.exists()


def test_columns_that_differ_from_the_declaration_stop_the_build(sample, tmp_path):
    cache, declaration = sample
    edited = yaml.safe_load(declaration.read_text(encoding="utf-8"))
    edited["tables"][0]["columns"][0]["name"] = "id"
    declaration.write_text(yaml.safe_dump(edited, allow_unicode=True), encoding="utf-8")
    out = tmp_path / "olist.sqlite"
    with pytest.raises(BuildError, match="differ from the declaration"):
        build(declaration, cache, out)
    assert not out.exists()
    assert not out.with_name(out.name + ".part").exists()


async def test_the_knowledge_tools_serve_it(sample, tmp_path, default_dataset):
    cache, declaration = sample
    out = tmp_path / "olist.sqlite"
    built = build(declaration, cache, out)
    settings = settings_for(
        default_dataset,
        tmp_path,
        knowledge_semantic_engine_enabled=True,
        knowledge_semantic_cache_dir=str(tmp_path / "semantic"),
    )
    target = KnowledgeMcp(settings)
    reg = registration(
        dataset_id=built["dataset_id"],
        data_version=built["data_version"],
        title=built["title"],
        bucket=BUCKET,
        object_key=f"samples/olist/{built['data_version']}/olist.sqlite",
        sha256=built["sha256"],
        size_bytes=built["size_bytes"],
        start_date=built["start_date"],
        end_date=built["end_date"],
        source_app=built["source_app"],
        source_ref=None,
        security_code=None,
    )
    target.catalog._registry = MemoryRegistry(
        registered(reg, by="service:knowledge-sample")
    )
    target.catalog._files = LocalFiles(tmp_path, content=out.read_bytes())
    grant = target.tokens.resolve(f"Bearer {TOKEN}")

    async def call(name: str, **arguments):
        reply = await target.handle(
            grant,
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {"name": name, "arguments": arguments},
            },
        )
        result = reply["result"]
        assert not result["isError"], result["content"][0]["text"]
        return result["structuredContent"]

    listed = await call("list_datasets")
    row = next(d for d in listed["datasets"] if d["dataset"] == "olist-ecommerce")
    assert row["security_code"] is None
    assert row["data_version"] == built["data_version"]

    schema = await call("describe_schema", dataset="olist-ecommerce")
    assert BUSINESS <= {t["name"] for t in schema["tables"]}
    assert "phys_" not in json.dumps(schema)

    metrics = (await call("metric_definitions", dataset="olist-ecommerce"))["metrics"]
    assert len(metrics) == 11
    assert all(m["queryable"] is False for m in metrics)

    # 取消的 o2 与没有明细的 o4 不算；c1、c2 是同一个真实客户
    rows = (await call("run_sql", dataset="olist-ecommerce", sql=MONTHLY_GMV))["rows"]
    assert rows == [
        {"period": "2017-01", "gmv": 18.0, "order_count": 1},
        {"period": "2017-02", "gmv": 23.0, "order_count": 1},
    ]
    repeat = await call(
        "run_sql",
        dataset="olist-ecommerce",
        sql=(
            "SELECT AVG(CASE WHEN n >= 2 THEN 1.0 ELSE 0.0 END) "
            "AS repeat_customer_share "
            "FROM (SELECT c.customer_unique_id, COUNT(*) AS n FROM orders o "
            "JOIN customers c ON c.customer_id = o.customer_id GROUP BY 1)"
        ),
    )
    assert repeat["rows"] == [{"repeat_customer_share": pytest.approx(1 / 3)}]
