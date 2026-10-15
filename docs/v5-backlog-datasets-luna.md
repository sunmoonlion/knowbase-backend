# v5 M1-204 / B5：摄入 Dataset 静态授权

2026-09-13，Luna 单人实施、自测；基线 `knowbase-backend@72f17d2`。
仅源码候选；未修改业务 dataset、Secret、部署或业务数据库。总处置清单位于 k8s
`sunmoonai/docs/v5-backlog-disposition-luna.md`。

## 1. 原缺口与本包选择

检索已有 RETRIEVAL_DATASET_ALLOWLIST，但摄入曾按调用方 dataset_key 查找同名 dataset，
没有找到就通过 RAGFlow 创建。可靠投递回执解决了重复副作用，**不构成写目标授权**。

本包增加独立 `INGESTION_DATASET_BINDINGS`：静态 `dataset_key → dataset_id/dataset_name`。
空配置默认 `{}`，拒绝全部摄入；不从检索白名单推导写权限，不默认放行 `default`，不按名字
接受任意 ID，不让请求携带 provider ID。Registry UI、多租户/逐主体 ACL 仍是未来产品任务。
当前在已有 Info 服务关系和 Knowledge Admin 身份范围内共用一份领域摄入白名单，
不是“任何登录用户都可用”，也不是客户间数据隔离方案。

配置示意（**占位值不是本轮核准的业务 ID，不能直接部署**）：

```json
{
  "market-news": {
    "dataset_id": "REPLACE_WITH_VERIFIED_EXISTING_ID",
    "dataset_name": "market-news"
  }
}
```

经现有环境配置注入 JSON 字符串；Settings 构造时严格验证：未知字段、重复 JSON 键、
错误类型、非法 key/ID/name、多个业务 key 别名到同一个 provider ID 均拒绝。
上限 128 项/64 KiB，ID 不接受路径字符；缺配置是明确禁用，不是自动补建。
仅新增这一项配置，无依赖、表、迁移、契约 DTO 或前端变更。

## 2. 授权在哪执行

| 路径 | 强制行为 |
| --- | --- |
| Admin / Internal submit | 共享 application 用例先核白名单，再创建 job/Outbox；未知 key 403、无写入 |
| 受理持久化 | 首条 accepted.status_history.metadata 的 ingestion_binding_v1 与 job/Outbox 同事务写入 |
| 重复请求 | 请求意图仍逐项对比；不能通过重复提交更新既有授权快照 |
| dispatch / retry | 非终态排队或重试必须核原快照与当前配置；force 不绕过策略 |
| Worker | 在 running、读原文或访问 Provider 前复核；配置撤销/改绑/缺历史快照拒绝 |
| RAGFlow dataset | 查找既有目标，名字和 ID 都必须匹配；不存在/重复/错 ID 不创建，不上传 |
| 回执恢复 | 在读制品/Provider 前复核作业授权，旧 upload intent 的 dataset_id 也必须匹配 |
| 最终领域绑定 | 再核结果 dataset_id/name 与授权，禁止错误 Provider binding 落入 KnowledgeDocumentVersion |
| Admin status metadata | 禁止写 ingestion_binding_v1，不能给空历史旧任务伪造受理授权 |

快照包含逻辑 key、ID、name、Provider endpoint 的 SHA-256；不存 API key，也不把带凭据
可能性的 endpoint 原文回显。只信服务端第一条 accepted 记录，不信 document.metadata、
后续 status metadata 或重试调用中的声明；这些客户端原始材料仍可留档，但没有授权效力。

每个作业的快照是受理时配置的证据，不是第二份可编辑策略主档。当前配置是准入规则；
快照与配置不一致时阻断，而不是二者选其一。既有 Provider operation 的 scope、
ID/回执一致性校验继续保留；即使新请求获准，也不能偷偷把同 key 历史回执改绑到新 ID。

RAGFlowClient.create_dataset 保留为兼容入口，但立即抛错，**不发 POST /datasets**。
上传和 parse 仍经原意图/回执/未知结果流程，不是禁止全部 Provider 写入。
旧“dataset 创建丢回执后不重复创建”测试按新边界改为“根本不允许数据面创建”；
upload/parse 丢回执、并发、lease/fencing 与事务测试继续保留。

## 3. 兼容与切换前置

1. 先只读核业务数据集的真实 ID/name、所属 Provider/租户及凭据可见性，由持权运维配置映射。
   本轮没有访问真实 RAGFlow，也没有替使用者选择业务 ID。
2. 盘点旧 accepted/running/reconciliation_required、重试代次和 Provider operation 回执。
   **旧无快照任务不自动补授权**；不因它的名字恰好在新配置中就恢复上传。
3. 控制切换要排空/停止旧 API 与 Worker，再用一致的新代码/配置启动全部角色。
   **静态 Settings 在进程中缓存，不是即时撤权广播。**旧进程不会自动读取新环境变量；
   新快照也不是能阻止旧二进制写入的数据库 fence。不能混跑旧 Worker 并宣称已撤权。
4. 受控核实未知 key 拒绝、合法 key 全链路与回执恢复；保持原服务/浏览器身份隔离。
   真实角色权限矩阵、配置分发和部署切换仍待 B7/N4 验收，未改变任何凭据。
5. 旧任务是否放弃、是否重建以及有副作用时怎样恢复，必须依据实际回执单独裁定。
   不改旧 idempotency key/source version 来绕过 unknown；本包没有批量回填或自动解除工具。

空映射不会让整个 Backend 启动失败，仍可查询诊断；但所有新摄入均 403。
RAGFlow config-check 的 ready 仍表示 Provider/embedding 健康，不表示摄入映射齐备。
即使 RAGFlow 未启用而只做 artifact_verified，摄入准入也要映射；之后启用/更换 endpoint
会改变快照，不能假设之前排队的任务自动取得新的远端写权限。

撤权在 Worker 前置检查抛出领域 ForbiddenError，不写 running/Inbox，也不把它伪装成
正常业务完成；公共投递按已有有界策略重试/死信，可在获准恢复后重放。
HTTP 的新增 403 仍通过现有错误格式返回；Info 客户端将其视为 HTTP 错误，不当成 accepted。
这是行为收紧，虽未修改 schema，仍需双端回归和配置切换，不能无配置直接上线。

## 4. 验证

- PostgreSQL 17.6 一次性 `knowledge_backlog_tests`，按测试随机 schema 升完整迁移链；
  RAGFlow、原文读取及身份在专项中注入，没有真实业务 Provider 操作。
- Knowledge 全套含共享契约向量 **173 passed / 0 skipped**（10.55 秒）；Ruff/Pyright 通过。
- Info 当前固定源码 `034b121` 的契约/分发用例 **6 passed**（0.94 秒）；另由 Knowledge
  HTTP 用例产生真实 ASGI 的 202/403 响应，经子进程加载实际 Info 客户端消费，验证不误接受。
  子进程隔离两仓 app/core 导入，不修改 Info 源码。测试要求相邻 Info 组件及其 venv 可用；
  缺失时明确 skip，不能称双端验证通过。
- 新用例覆盖空/非法配置、未知 key 无写、两面一致、客户端/状态 metadata 伪造、
  Worker 撤权/改 ID/换 endpoint、force retry/dispatch、无快照旧任务、恢复撤权、
  同名错 ID、最终落库拒绝错误目标、底层兼容创建入口无 HTTP；原投递故障用例仍通过。
- 无业务配置/存量任务扫描、真实 RAGFlow 目标确认、生产 IAM/CSRF 全旅程、KIND 或镜像验证。
  单人自测不是独立多方验收，源码同步不是部署。

回滚：使用反向代码提交，无数据库 downgrade；保留受理快照和 Provider 回执，不清表。
旧程序会重新开放数据集自动创建及忽略本包授权，因此回退前须停写评估，不能把代码可回退
等同安全边界可放宽。B6 解析调度、B7 运维/存量切换和 B8 新计划移交仍未完成。
