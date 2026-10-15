# 语义层

> 设计：k8s 库 `tree-build/SDD/modules/0009-semantic.md`。本文是甲段（2026-09-27）的实现说明。

MCP 工具后面的检查、规划、执行可以走语义层。引擎是 WrenAI（`wrenai==0.15.0`，钉死），当成库用，只用它的规划。
开关默认关；关着时知识服务与以前完全一样。

## 配置

| 配置（环境变量同名大写） | 默认 | 含义 |
| --- | --- | --- |
| `knowbase_semantic_engine_enabled` | `false` | 打开后，默认数据集与登记的数据集都走语义层 |
| `knowbase_semantic_cache_dir` | `/tmp/knowledge-semantic` | 转换出的库放这里，文件名 `<数据集文件的 sha256>-b<生成规则版本>.duckdb` |

缓存目录要可写。数据集文件本身只读，不会被改动。

## 一条查询的路

```text
run_sql 收到 SQL
  → 前置检查  application/services/semantic_guard.py   按语法树
  → 规划      infrastructure/semantic/wren_planner.py   引擎，严格模式
  → 执行      infrastructure/semantic/duckdb_store.py   我们自己的连接
  → 类型规整、带上出处
```

三层各自独立拦截，任何一层漏了，后面的仍然拦得住：

| 层 | 拦什么 |
| --- | --- |
| 前置检查 | 不是单条查询的一切语句；带库名或模式名的表引用；物理表；表函数；引号里的文件路径；读文件、读环境、读密钥的函数；过长的 SQL |
| 引擎 | 语义模型里没有的表；被禁的函数；规划后的 SQL 里出现写操作 |
| 库 | 只读挂载；`enable_external_access=false`；`lock_configuration=true`；不自动装载扩展；内存 512 MB、两个线程；超时中断 |

## 数据集怎么准备

第一次用到某个数据集时：

1. 读自述：表、字段与类型、`field_dictionary`、`metric_dictionary`、`dataset_metadata`。
2. 转换成 DuckDB 文件。物理表名是 `phys_` 加原表名；类型照搬（整数、小数、文字），日期保持文字。
3. 生成语义模型：每张表一个模型，模型名是原表名。
4. 已有的缓存文件用之前核对每张表的行数，对不上或打不开就重建。

600009 的数据集（143 KB）准备约 2 秒，零售库（21 MB）约 1.5 秒；之后打开 0.1 秒以内。

不合约定的数据集上不了语义层，工具答复 `dataset unavailable`，原因进日志：

| 情况 |
| --- |
| 表名或字段名不是普通标识符；表名以 `phys_` 开头；名字只有大小写不同 |
| 字段的声明类型不是整数、小数、文字三类（例如没有声明类型、`BLOB`、`NUMERIC`、`DATE`） |
| 字段里的值与声明类型对不上 |

## 与旧路的差别

| 项 | 旧路（SQLite） | 语义层 |
| --- | --- | --- |
| 方言 | SQLite | DuckDB |
| 整数相除 | 取整（`1/2` 得 0） | 得小数（`1/2` 得 0.5）；取整用 `//` |
| 系统表 | 查得到 `sqlite_master` | 查不到任何系统目录 |
| `describe_schema` 的字段 | `name`、`type`、`primary_key` | `name`、`type`、`display_name`、`unit`；另有 `sql_dialect` |
| 出处 | 六项 | 多一项 `engine` |
| 结果里重名的列 | 后一个覆盖前一个 | 拒绝，要求起别名 |

两份评测题的真值查询（财务 15 条、零售 31 条）在两条路上结果逐项相同，有测试守着。

## 升级引擎

引擎只在 `infrastructure/semantic/wren_planner.py` 里被引入。升级步骤：改 `pyproject.toml` 里钉的版本与
`wren_planner.py` 里的 `ENGINE`，跑 `tests/test_semantic_*.py`。生成规则有变时把 `domain/semantic.py` 的
`BUILDER_VERSION` 加一，旧缓存自动作废。

## 测试

| 文件 | 验什么 |
| --- | --- |
| `tests/test_semantic_guard.py` | 前置检查：放行的写法、各类拒绝 |
| `tests/test_semantic_layer.py` | 自述、语义模型、转换的保真与可重复、执行的只读与超时与并发、类型规整、报错清理、真值查询新旧一致、攻击写法、缓存 |
| `tests/test_semantic_mcp.py` | 开关关着与以前一样；开着时两个数据集都走语义层 |

零售库不进 git；它在 `app/datasets/` 下时，31 条那一项才跑，否则跳过。

## 按口径名查询（乙段，2026-09-27）

工具 `query_metric`，只在语义层开着时存在。专家报口径名，值由口径的定义算出，是否适用由定义里的条件判定。

```json
{
  "dataset": "sh600009-financials",
  "metrics": ["gross_margin", "invest_income_share"],
  "filters": [
    {"column": "report_type", "op": "eq", "value": "年报"},
    {"column": "fiscal_year", "op": "gte", "value": 2019}
  ],
  "order_by": [{"by": "fiscal_year", "direction": "asc"}]
}
```

每行结果是基础表的一行（财务报表：一家公司的一个报告期），带主键与说明字段（含口径 `basis`），
每个口径给出 `value`、`unit`、`applicable`、`reason`。另返回每个口径的定义、实际执行的 SQL、出处。

| 情况 | 结果 |
| --- | --- |
| 定义里的条件不成立（例如营业利润为负） | `applicable` 为假，`value` 为空，`reason` 是数据集给的原因 |
| 条件成立但所需数据是空的 | `applicable` 为真，`value` 为空，`reason` 为「所需数据缺失」 |
| 一次查询里的口径基础表不同 | 拒绝，要求分开查 |
| 口径只有说明、不能按名查询（`roe_avg`） | 拒绝，并列出可查的口径 |
| 数据集没有第二版自述（零售库、第一版财务数据集） | 拒绝，答复改用 `metric_definitions` 加 `run_sql` |

口径的定义来自数据集自己（knowfeed 的数据集自述第二版，见 knowfeed-backend 的 `docs/SECURITY_INGESTION.md`）。
知识服务把它当数据看：表达式先按语法树校验，只许字段、常数、四则运算、比较、逻辑、`CASE`、
`COALESCE`、`NULLIF`、`ABS`、`ROUND`；字段必须是基础表的，或是与基础表声明了关系的表的。
不合规的口径记日志、不提供，不影响别的口径；`metric_definitions` 里它的 `queryable` 为假。

筛选与排序只能用基础表的主键与说明字段；筛选的值是数据，用语法树拼进查询，不做字符串拼接。
生成的查询与专家手写的查询走同一条路（前置检查、规划、执行）。

现在只有逐行的口径。汇总类的口径（零售库那种按维度求和）要用引擎的口径集，等有数据集带这类定义时再做。

## 测试（乙段）

`tests/test_semantic_metrics.py`：自述第二版的读取与校验；表达式的放行与拒绝；九个可查询的口径在
基础表每一行上的结果与手写公式一致（含跨表的一个）；不适用与数据缺失；筛选、排序、截断；
筛选值里的注入写法；工具的有无随开关；令牌的工具清单。

## 还没做的

专家包与评测的配合是丙段；部署是丁段。
