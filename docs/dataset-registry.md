# 多数据集与数据集登记

> 设计：k8s 库 `tree-build/SDD/modules/0008-info.md` 段三。2026-09-27。

知识服务原先只装一个数据集（第 23 课零售库，由配置钉住路径与 sha256）。现在可以再登记别的数据集，
MCP 的三个查询工具用可选参数 `dataset` 选择；不带参数时仍是默认数据集，问数专家包与二十题不受影响。

## 开关与配置

| 配置（环境变量同名大写） | 默认 | 含义 |
| --- | --- | --- |
| `knowledge_dataset_registry_enabled` | `false` | 关着时：登记接口返回 404，`list_datasets` 只列默认数据集 |
| `knowledge_dataset_allowed_buckets` | 空 | 逗号分隔。登记的对象必须在这些桶里；空就是一个都不许 |
| `knowledge_dataset_cache_dir` | `/tmp/knowledge-datasets` | 取回的文件放这里，文件名是 `<sha256>.sqlite` |

取文件用服务自己的 `S3_*` 凭据，所以知识服务的存储账号要对来源方的桶有**只读**权限（部署时配，段五）。

## 登记接口

`POST /api/internal/v1/knowledge/datasets`，服务间令牌（与入库同一个权限 `require_knowledge_ingest_service`）。

```json
{
  "dataset_id": "sh600009-financials",
  "data_version": "sh600009-financials-39a395bfa6f16b67",
  "title": "上海机场 财务报表",
  "security_code": "600009",
  "object": "s3://<bucket>/info/securities/code=600009/datasets/<data_version>/sh600009-financials.sqlite",
  "object_version_id": null,
  "sha256": "<64 位小写十六进制>",
  "size_bytes": 143360,
  "start_date": "1994-12-31",
  "end_date": "2026-06-30",
  "source_app": "info",
  "source_ref": "<采集批次标识>",
  "quality_passed": true
}
```

| 情况 | 结果 |
| --- | --- |
| 新版本 | 201；它成为现行版本，原现行版本变为 `superseded` |
| 同一版本、同一内容再登记 | 201，不新增行，登记人与时间不变（幂等） |
| 同一版本、内容或位置不同 | 409 |
| 重新登记一个被取代的旧版本 | 201，旧版本重新成为现行版本（回退用） |
| `quality_passed` 为假、桶不在允许清单、键含 `..`、标识是默认数据集的标识等 | 422 |
| 开关关着 | 404 |

规则：

- `data_version` 必须以 `dataset_id` 开头；版本由来源方按内容定，知识服务不改。
- 登记人取自已验证的服务身份，请求体里不收。
- 文件不经接口上传。知识服务按登记的位置自己取，取到的内容与 `sha256` 对不上就不用，也不留文件。
- 同一数据集的登记用事务级咨询锁串行化；数据库另有「每个数据集至多一个现行版本」的唯一索引兜底。

`GET /api/internal/v1/knowledge/datasets` 列出现行版本，不返回对象位置。

## MCP

| 工具 | 变化 |
| --- | --- |
| `list_datasets` | 新增。返回 `dataset`、`title`、`security_code`、`data_version`、`start_date`、`end_date`、`default` |
| `describe_schema`、`metric_definitions`、`run_sql` | 增加可选参数 `dataset` |

- 令牌里的工具清单照旧过滤（`F-KNOW-01`）：没被授予 `list_datasets` 的令牌列不了，但可以带 `dataset` 查。
- 未知的数据集是工具错误，文字里提示先调 `list_datasets`。专家据此答复「未入库」，不猜。
- 登记表每 30 秒重读一次；新登记的数据集最迟 30 秒后可见。
- 登记表或对象存储出故障时，对模型只说「暂时不可用」，细节只进日志。
- 只读保护（只许单条 `SELECT`/`WITH`，行数与时间封顶）对所有数据集一样。

`app.bootstrap.mcp` 那个只挂 MCP 的最小应用不初始化数据库；在它上面打开登记开关，
`list_datasets` 会报暂时不可用。要用多数据集请起主应用。

## 数据集文件的约定

与默认数据集同形：`dataset_metadata`（至少有 `data_snapshot_id`、`start_date`、`end_date`）、
`metric_dictionary`，其余是业务表。`tests/fixtures/sh600009-financials.dataset.bin` 是 info 实采实建的
600009 数据集原件（后缀不用 `.sqlite` 是因为仓库忽略该后缀），测试用它验证两边的约定对得上。

## 样例数据集

> 设计：k8s 库 `tree-build/SDD/modules/0023-fin-forecast.md` 第三节。2026-10-13。

`olist-ecommerce`：巴西电商 Olist 的九张表（订单、明细、支付、评价、商品、客户、卖家、地理、品类译名），按真实客户 **1/20 抽样**（约 4900 单，文件约 4 MB；所有者 2026-10-13 定），给专家「财务分析与预测」演示与评测用。入样客户的全部订单连同明细、支付、评价都在，商品、卖家只留被用到的，地理表按邮编前缀汇总成一行；数据内部完整，绝对数约为全量的 1/20。

**许可证**：CC BY-NC-SA 4.0，只作演示与评测，不得商用。标题、`dataset_metadata` 的 `license` 与 `source` 两行都写着。

| 处 | 是什么 |
| --- | --- |
| `app/datasets/olist/olist.yml` | 唯一定义：九个 parquet 的地址与 sha256、表名、主键、关系、字段字典（中文名、单位、WrenAI 英文说明）、口径、说明 |
| `app/scripts/build_olist_dataset.py` | 读定义与全量 parquet，按定义里的 `sample` 抽样，写出与本节上文同形的 SQLite，按知识服务自己的读法自检；打印一行登记要用的 JSON。同一台机器上两次生成逐字节相同 |
| `app/tests/test_olist_dataset.py` | 用合成数据测抽样、生成、自述、版本与四个查询工具 |

生成（parquet 缺了会下载，sha256 不对就停；parquet 与生成出的文件都不进 git）：

```bash
cd app
uv run python -m scripts.build_olist_dataset --cache <parquet 目录> --out <文件>.sqlite
```

- `data_version` 按输入算：定义文件与九个 parquet 的 sha256，加生成器版本。改了定义文件，版本跟着变，重新生成后要重新登记。
- 口径全部是按维度汇总的，`queryable` 全为 0：`metric_definitions` 给定义，专家按定义用 `run_sql` 算；`query_metric` 不适用。
- 登记照上文「登记接口」：对象放知识服务自己的样例桶（桶要在 `knowledge_dataset_allowed_buckets` 里），`source_app` 为 `knowledge-sample`，`security_code` 不填。生产上用哪个桶、谁持有登记令牌，发布时定。
