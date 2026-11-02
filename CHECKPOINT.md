# 2026-11-02 luna：knowbase 既有类型与交付边界修复（未发布）

基线为 WP-3 提交 fe184e4。修复此前 27 个 pyright 错误及既有 lint 错误，没有关闭检查或增加忽略：补齐端口与导入、复用读法查询/映射、按 SQLAlchemy DML 结果类型读取行数、明确可空值；删除误缩进在函数内部且从未被调用的重复 record_preference，沿用已有 save_preference。

实际行为修复：订阅到期明确返回 subscription_expired/403；非公开采集的 multipart 错误与空受益人明确 422；JSON 日期转换为日期值；第一次创建资料与加工依据在同一事务写入，重复投递不重新授予已撤回的依据；重试已删除/禁用资料拒绝。用合成 CSV 和隔离数据库测试，没有真实渠道取数。

验证：ruff 全仓通过；pyright 0 errors/0 warnings；全量 723 passed、8 skipped（/tmp/luna-knowbase-repair-full-tests.txt）；最后新增空受益人校验后，边界用例 9 passed（/tmp/luna-knowbase-boundary-final.txt）。此前定向 51 项通过中唯一失败是新测试调用 withdraw_use 时参数少一项，修正测试后权限回归通过；不把失败输出删除。

剩余：非公开链尚不能声明端到端验收通过。当前 has_readings 布尔只代表存在已完成读法，knowfeed 的旧分支会把 true 当成请求的全部读法齐全；后续必须以逐读法清单/完成状态的版本化契约补齐生产者与消费者、部分加工恢复和并发幂等测试，完成后再考虑 Wind W1。此提交不接 Wind、不改已发布巨潮保底、不会自动发版。

---

## 2026-11-02 逐读法回执（feat/reading-receipts，未发布）

在 5bfb60f 上为 collected-originals 增加兼容字段 readings/renditions：按每个受益人的内容版本返回各类型最新 ready 读法及编号、摘要、处理器版本。pending/failed 不作为就绪；旧 has_readings 仍只表达任一种已就绪。旧契约所有已有定义保持一致，新增两个响应模型并补入 provider schema，knowfeed consumer lock 同步。

完整回归 730 passed / 5 skipped（/tmp/luna-readings-base-full.txt），ruff/pyright 通过。HTTP + 真实库测试证明：两个受益人分别存储，一人的读法不会出现在另一人的回执；重复交付不改变资料 ID；多版本取最新 ready，较新的 failed 不覆盖仍可用的 ready，遵循现有解析器语义。

这不是完整非公开链或 Wind 验收。跨进程并发首次建副本、真实升级恢复仍需下一工作单元验证。qwen 当前 fix/supply-delivery 候选不动；这里只提交独立开发分支，不改部署源。

---

# 2026-11-02 luna：WP-3 公共原件收录核验（未发布）

对照《财务智能体双轨交付计划》WP-3，复核旧 qwen 5a22a91 后没有整段合并：其 period 会把披露年算报告年，公开条件混用许可字段，且带入无关端口改动。现复用旧意图，在当前发布基线上实现管理端只读 GET /api/admin/v1/materials/holdings。

查询按主体代码、资料细类、报告期双端点、版本形态精确匹配；年份按统一词表的定期报告规则展开，支持明确 ISO 日期区间；默认全文，摘要不能证明全文已收录。仅公共原件主体且 content_origin=disclosure、未删除/禁用/清除的主档，回查询时刻、科目表版本和当前内容版本/摘要，不回内容。非法查询回 422，管理端鉴权沿用既有机制。不查询 Wind，不修改演示任务或采单。

验证：定向 34 passed；含真实隔离 PostgreSQL 迁移的全量 717 passed、8 skipped（/tmp/luna-wp3-full-tests.txt）。新增覆盖当前版本、摘要/全文、报告年/披露年、主体、类型、期间双端点、三种私有许可、删除/禁用/清除、非法输入与管理身份。应用层基线 pyright 27 个既有错误，新增后仍 27 个；全仓 ruff 亦有既有错误，不宣称质量门全绿。待单独清理基线问题，再经正式发布链上线。环境已 uv sync --frozen，数据库 luna_knowbase_tests（仅隔离测试 schema）。

正式说明同步到 sunmoonun 0043 第 2.4 节及双轨主账。父仓与发布来源仍以 #46 对齐基线为准；本提交属于 luna 第二轨开发，未作为保底演示的一部分。

---

## 2026-11-02 清单基版并发保护（未发布）

基线 a69436bc440624e87f81974f1ced05f6302505cd，继续 feat/reading-receipts。
既有修订 advisory lock 原来只分配序号，不能防止基于过期快照的写入覆盖新需求。
仓储 add_resolution 现在必须传 expected_revision，在原短事务内比较最新修订：
初建为 0，追加/整份修改/重新解析传读取的基版。冲突返回 409 resolution_changed，
不生成新修订、不覆盖赢家。无新增表、迁移或网络字段，不在锁内做外部解析。

三个隔离 PostgreSQL 故障场景（慢追加、慢修改、慢刷新遇到先完成的追加）在原代码
均失败，修复后保留赢家；显式重读重试追加会保留双方需求。原复现输出
/tmp/luna-resolution-cas-before.txt，定向 21 passed：/tmp/luna-resolution-cas-focused.txt。
全量 **733 passed / 5 skipped**，301.67 秒：/tmp/luna-resolution-cas-full.txt；
全仓 ruff 通过，pyright 0 errors（/tmp/luna-resolution-cas-ruff-final.txt、
/tmp/luna-resolution-cas-pyright.txt）。初次两处注释超长已修，不隐藏原 lint 输出。

本改动只防解析期间发生的并发覆盖；没有给网页新增用户所见修订令牌，不把旧页面
事后提交或 HTTP 回包丢失变成幂等操作。同 need key 重放仍拒绝。消费者收到冲突须
重读并明确重试，不当成功、不自动确认采集。完整自动恢复另待工作单元。
工作台配套在其 feat/method-execution-view；发布要评审本分支此前 WP-3/逐读法差异，
不能直接覆盖 qwen 已冻结候选。没有 Jenkins、sources、生产迁移或 Wind 调用。

