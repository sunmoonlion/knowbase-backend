"""生成样例数据集 olist-ecommerce。

设计：k8s 仓 tree-build/SDD/modules/0023-fin-forecast.md 第三节。
读 datasets/olist/olist.yml 与九个 parquet，写出与 info 产出的数据集
同形的 SQLite：业务表，加自述表 dataset_metadata、field_dictionary、
metric_dictionary、table_links、table_keys。
写完按知识服务自己的读法自检一遍，再让引擎规划一条查询；最后打印一行 JSON 供登记。

许可证：Olist 巴西电商数据集是 CC BY-NC-SA 4.0，只作演示与评测，不得商用。

用法（在 knowledge-backend/app 下）：
    uv run python -m scripts.build_olist_dataset \
        --cache <parquet 目录> --out <SQLite 文件>

缺 parquet 就下载到 --cache；已有文件或下载结果的 sha256 与定义不符就停，不覆盖。
data_version 按输入算（定义文件与九个 parquet 的 sha256、生成器版本），
同样的输入得到同样的版本。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
import sys
import time
import urllib.request
from pathlib import Path
from typing import Any

import duckdb
import yaml

BUILDER_VERSION = "1"
DECLARATION = Path(__file__).resolve().parents[1] / "datasets" / "olist" / "olist.yml"
CHUNK = 50_000
DOWNLOAD_TIMEOUT = 300


class BuildError(Exception):
    """输入与定义对不上。消息给操作的人看。"""


def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def load_declaration(path: Path) -> dict[str, Any]:
    with path.open(encoding="utf-8") as f:
        declaration = yaml.safe_load(f)
    if not isinstance(declaration, dict) or not declaration.get("tables"):
        raise BuildError(f"declaration has no tables: {path}")
    return declaration


def data_version(declaration: dict[str, Any], declaration_sha256: str) -> str:
    lines = [f"builder {BUILDER_VERSION}", f"declaration {declaration_sha256}"]
    lines += sorted(f"{t['source']} {t['sha256']}" for t in declaration["tables"])
    digest = hashlib.sha256("\n".join(lines).encode()).hexdigest()
    return f"{declaration['dataset_id']}-{digest[:16]}"


def fetch_sources(declaration: dict[str, Any], cache: Path) -> dict[str, Path]:
    """每张表的 parquet：缓存里有就核对，没有就下载后核对。"""
    cache.mkdir(parents=True, exist_ok=True)
    paths: dict[str, Path] = {}
    for table in declaration["tables"]:
        path = cache / f"{table['source']}.parquet"
        if not path.exists():
            url = declaration["base_url"] + path.name
            partial = path.with_suffix(".parquet.part")
            with urllib.request.urlopen(url, timeout=DOWNLOAD_TIMEOUT) as response:  # noqa: S310 地址来自定义文件
                partial.write_bytes(response.read())
            if sha256_of(partial) != table["sha256"]:
                partial.unlink()
                raise BuildError(f"downloaded file does not match its sha256: {url}")
            partial.replace(path)
        elif sha256_of(path) != table["sha256"]:
            raise BuildError(
                f"cached file does not match its sha256: {path}; "
                "delete it to download again"
            )
        paths[table["name"]] = path
    return paths


def sqlite_type(table: str, column: str, source_type: str) -> tuple[str, bool]:
    """（SQLite 声明类型，是否时间）。认不出的类型不猜。"""
    kind = source_type.upper()
    if kind.startswith("TIMESTAMP") or kind == "DATE":
        return "TEXT", True
    if kind in ("BIGINT", "INTEGER", "SMALLINT", "TINYINT", "HUGEINT"):
        return "INTEGER", False
    if kind in ("DOUBLE", "FLOAT", "REAL") or kind.startswith("DECIMAL"):
        return "REAL", False
    if kind == "VARCHAR":
        return "TEXT", False
    raise BuildError(f"unsupported source type {source_type}: {table}.{column}")


def quoted(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def copy_table(
    duck: duckdb.DuckDBPyConnection,
    target: sqlite3.Connection,
    table: dict[str, Any],
    source: Path,
) -> None:
    escaped = str(source).replace("'", "''")
    described = duck.execute(
        f"DESCRIBE SELECT * FROM read_parquet('{escaped}')"  # noqa: S608 路径来自本地缓存
    ).fetchall()
    actual = [str(row[0]) for row in described]
    declared = [str(c["name"]) for c in table["columns"]]
    if actual != declared:
        raise BuildError(
            f"columns of {table['source']} differ from the declaration: "
            f"{actual} != {declared}"
        )
    columns: list[str] = []
    selects: list[str] = []
    for name, source_type in ((str(r[0]), str(r[1])) for r in described):
        declared_type, is_time = sqlite_type(table["name"], name, source_type)
        columns.append(f"{quoted(name)} {declared_type}")
        selects.append(
            f"strftime({quoted(name)}, '%Y-%m-%d %H:%M:%S')"
            if is_time
            else quoted(name)
        )
    target.execute(f"CREATE TABLE {quoted(table['name'])} ({', '.join(columns)})")
    insert = (
        f"INSERT INTO {quoted(table['name'])} VALUES "  # noqa: S608 表名来自定义文件
        f"({', '.join('?' for _ in columns)})"
    )
    cursor = duck.execute(
        f"SELECT {', '.join(selects)} FROM read_parquet('{escaped}')"  # noqa: S608
    )
    while rows := cursor.fetchmany(CHUNK):
        target.executemany(insert, rows)


def date_range(
    target: sqlite3.Connection, declaration: dict[str, Any]
) -> tuple[str, str]:
    spec = declaration["date_range"]
    row = target.execute(
        f"SELECT MIN(substr({quoted(spec['column'])}, 1, 10)), "  # noqa: S608
        f"MAX(substr({quoted(spec['column'])}, 1, 10)) FROM {quoted(spec['table'])}"
    ).fetchone()
    if row is None or row[0] is None:
        raise BuildError("date range column is empty")
    return str(row[0]), str(row[1])


def write_description(
    target: sqlite3.Connection,
    declaration: dict[str, Any],
    version: str,
    start: str,
    end: str,
) -> None:
    target.execute("CREATE TABLE dataset_metadata (key TEXT, value TEXT)")
    metadata = [
        ("data_snapshot_id", version),
        ("start_date", start),
        ("end_date", end),
        *((str(k), str(v)) for k, v in declaration["metadata"].items()),
        *(
            (f"source_{t['name']}", f"{t['source']}.parquet sha256={t['sha256']}")
            for t in declaration["tables"]
        ),
    ]
    target.executemany("INSERT INTO dataset_metadata VALUES (?, ?)", metadata)

    target.execute(
        "CREATE TABLE field_dictionary "
        "(source_table TEXT, field TEXT, display_name TEXT, unit TEXT, "
        "description TEXT)"
    )
    target.executemany(
        "INSERT INTO field_dictionary VALUES (?, ?, ?, ?, ?)",
        [
            (
                t["name"],
                c["name"],
                c["display_name"],
                c.get("unit"),
                c.get("description"),
            )
            for t in declaration["tables"]
            for c in t["columns"]
        ],
    )

    target.execute(
        "CREATE TABLE metric_dictionary (metric_name TEXT, display_name TEXT, "
        "source_table TEXT, expression_hint TEXT, unit TEXT, time_basis TEXT, "
        "description TEXT, kind TEXT, base_table TEXT, value_expression TEXT, "
        "applicable_when TEXT, reason_if_not TEXT, queryable INTEGER, row_filter TEXT)"
    )
    target.executemany(
        f"INSERT INTO metric_dictionary VALUES ({', '.join('?' * 14)})",
        [
            (
                m["metric_name"],
                m["display_name"],
                m["base_table"],
                m["expression"],
                m["unit"],
                m["time_basis"],
                m["description"],
                m["kind"],
                m["base_table"],
                m["expression"],
                "",
                m.get("reason_if_not", ""),
                0,  # 汇总口径与缺的口径都不能按名查询，由专家按定义写 SQL
                m["row_filter"],
            )
            for m in declaration["metrics"]
        ],
    )

    target.execute(
        "CREATE TABLE table_links (link_name TEXT, from_table TEXT, to_table TEXT, "
        "cardinality TEXT, on_columns TEXT)"
    )
    target.executemany(
        "INSERT INTO table_links VALUES (?, ?, ?, ?, ?)",
        [
            (k["name"], k["from"], k["to"], k["cardinality"], ",".join(k["columns"]))
            for k in declaration["links"]
        ],
    )

    target.execute(
        "CREATE TABLE table_keys "
        "(table_name TEXT, key_columns TEXT, label_columns TEXT)"
    )
    target.executemany(
        "INSERT INTO table_keys VALUES (?, ?, ?)",
        [
            (t["name"], ",".join(t["key"]), ",".join(t.get("labels", ())))
            for t in declaration["tables"]
            if t.get("key")
        ],
    )


def self_check(path: Path) -> None:
    """按知识服务的读法读一遍，生成语义模型，让引擎规划一条查询。"""
    from app.application.services.semantic_model import build_manifest
    from app.infrastructure.semantic.sqlite_source import read_description
    from app.infrastructure.semantic.wren_planner import WrenPlanner

    manifest = build_manifest(read_description(path))
    WrenPlanner(manifest).plan("SELECT COUNT(*) AS n FROM orders")


def build(declaration_path: Path, cache: Path, out: Path) -> dict[str, Any]:
    declaration = load_declaration(declaration_path)
    version = data_version(declaration, sha256_of(declaration_path))
    sources = fetch_sources(declaration, cache)

    out.parent.mkdir(parents=True, exist_ok=True)
    partial = out.with_name(out.name + ".part")
    partial.unlink(missing_ok=True)
    duck = duckdb.connect(":memory:")
    target = sqlite3.connect(partial)
    try:
        for table in declaration["tables"]:
            copy_table(duck, target, table, sources[table["name"]])
        start, end = date_range(target, declaration)
        write_description(target, declaration, version, start, end)
        target.commit()
    except BaseException:
        target.close()
        partial.unlink(missing_ok=True)
        raise
    finally:
        target.close()
        duck.close()
    os.replace(partial, out)
    self_check(out)
    return {
        "dataset_id": declaration["dataset_id"],
        "data_version": version,
        "title": declaration["title"],
        "source_app": declaration["source_app"],
        "sha256": sha256_of(out),
        "size_bytes": out.stat().st_size,
        "start_date": start,
        "end_date": end,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="生成样例数据集 olist-ecommerce")
    parser.add_argument("--cache", type=Path, required=True, help="parquet 所在目录")
    parser.add_argument("--out", type=Path, required=True, help="生成的 SQLite 文件")
    parser.add_argument("--declaration", type=Path, default=DECLARATION)
    args = parser.parse_args(argv)
    started = time.monotonic()
    try:
        result = build(args.declaration, args.cache, args.out)
    except BuildError as exc:
        print(f"build failed: {exc}", file=sys.stderr)
        return 2
    print(f"built in {time.monotonic() - started:.1f}s", file=sys.stderr)
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
