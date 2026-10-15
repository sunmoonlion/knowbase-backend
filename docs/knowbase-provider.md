# Knowledge Provider 内部接口

2026-09-13 源码解耦；默认仍为 RAGFlow，不代表已接 WeKnora 或已部署。

## 分工

- `application/ports/knowledge_provider.py`：数据面 Port、类型化 dataset/document/chunk、
  解析状态和异常。Port 不拥有数据库事务，不提供 dataset 创建、删除或自动切换。
- `infrastructure/external/knowledge_provider.py`：配置与实现装配，仅允许当前 RAGFlow。
- `ragflow_provider.py`：HTTP 字段/数字状态/身份范围转换，底层沿用 `ragflow.py`。
- `artifact_content.py`：独立的 S3 不可变制品读取与校验，不依赖索引供应商。
- `application/services/provider_delivery.py`：原意图/回执/未知结果状态机，继续依赖
  PostgreSQL 事务、租约 fencing 和持久单次轮询，不将这些责任下放给适配器。

## 适配器的义务

精确查找必须拒绝截断结果；远端文档需核对 ID、dataset、文件名和实际原文字节哈希。
上传/解析不擅自重试，结果未知交回应用层对账；无确认回执不能假定操作从未发生。
解析状态归一为 `ParseStatus`，无法识别时为 UNKNOWN 并阻断，不当成功或永远轮询。
检索结果规范化后仍须由 Knowledge 按受权完整 binding 和 tenant 过滤，再生成领域引用。
HTTP 超时/协议/不可用继承统一错误；取消透传，所有调用路径释放客户端。
诊断 metadata 不参与授权和状态判定；非有限检索分数归零，不进入有效相关度。

## 本次保留的兼容边界

原 provider scope 摘要、endpoint 摘要、dataset/upload/parse 意图键、回执和轮询游标
不重键，不新增数据库迁移。ParseStatus 序列化沿用原游标值，旧运行中任务可继续恢复。
低层 RAGFlow 异常保留类名，继承统一 Provider 异常；原文错误改为独立 ArtifactError。
旧裸摄入入口继续失败关闭，config-check 仍是显式的 RAGFlow 专用诊断接口。

任务状态、历史 metadata、`ragflow_document_id` 和 completion helper 名称保留旧接口。
retrieval v1 schema / DTO 的 provider 仍限定 ragflow：内部可注入另一种测试 Provider，
但当前产品入口不会因此允许切换。WeKnora 落地需显式扩展对外契约并验证消费者，
安排旧索引/绑定/引用迁移与回滚；不能复用旧回执到另一供应商，不声称热插拔完成。

## 验证

`tests/test_knowledge_provider.py` 守住适配、异常、资源关闭和禁止业务重新依赖
RAGFlow 客户端的结构边界；`tests/test_provider_port_db.py` 用非 RAGFlow 假实现
运行真实回执/Inbox，验证重放、未知上传恢复与 scope 变化拒绝。
既有授权、投递、轮询、进程死亡、Artifact/检索契约测试继续执行，不能用假实现
代替真实 Provider 联调。跨仓固定提交与实际回执在 k8s 的
`sunmoonai/docs/legacy-backlog/verification-index.md` 的“Knowledge Provider 内部解耦”节；
旧实施报告按该索引中的固定 Git 版本获取，不作为当前工作指令。
