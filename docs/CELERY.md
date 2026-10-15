# Knowbase Backend 异步任务与运行角色

`knowbase-backend` 的 API、Worker、Scheduler、Migration 来自同一源码和不可变镜像，分别通过 `app.bootstrap.api`、`app.bootstrap.worker`、`app.bootstrap.scheduler`、`app.bootstrap.migration` 启动。

Worker 不是独立源码项目。知识摄取由 API 将 job 与 Outbox 命令同事务提交，API 不直接发布 Celery 消息。Scheduler 每 5 秒触发公共 Outbox 发布/对账，Worker 持有投递与消费权限，执行 `knowledge.ingest.v1` 并在领域提交后写 Inbox。各角色必须使用不同 ServiceAccount、Secret、资源与伸缩策略。

旧 `app.tasks.knowledge_ingestion` 和裸 RAGFlow 摄取入口拒绝执行，不能与公共路径混跑。上传结果未知不能盲目重试；迁移、回执认领、死信重放与回滚边界见 [可靠投递与 RAGFlow 回执](durable-delivery-luna.md)。

新 K8s 资源由 `tpl-app/k8s-scaffold-v2` 的统一运行角色模型生成。旧 `celeryworker-knowbase-admin-backend` 与 `nodebullworker-knowbase-web-backend` 只属于 v1 回滚拓扑，在 R5/R7 门禁前保留，但不得作为新源码或新部署生成器。
