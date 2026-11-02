# 2026-11-02 #49 重新核对采单修复候选

基线 fb88c486（#49 仍在使用）。补齐本人令牌 POST /api/internal/v2/user-data/resolutions/{id}/refresh，复用服务端重解析；用户及应用隔离，已选资料不动，无变化不新增修订，不确认、不授权、不采集。

定向含真实 knowbench 客户端 → HTTP → knowbase → 隔离 PostgreSQL：34 passed。未触碰线上任务；用户暂停验收状态不变，正式发布交 qwen。

发布基线全仓静态检查暴露既有未定义导入/端口不一致/交付边界问题。本分支移植开发分支 5bfb60f 的修复，解决冲突时剔除其父提交 WP-3 public_holdings 功能，不带入第二轨新接口。全量验证结果见下节。

## 完成的验证与边界

最终全量 714 passed / 8 skipped（/tmp/luna-recheck-provider-tests-final.log），ruff 全仓通过，pyright 0 errors / 0 warnings。真实 knowbench 客户端→loopback HTTP→实际 knowbase 路由→隔离 PostgreSQL 的测试包含在内并实际运行，未使用 refresh 替身；身份验证依赖为测试替身，不据此声称生产 OIDC/TLS 端到端通过。

首跑 712 passed / 8 skipped / 2 failed 的失败都是隔离工位缺已发布管理端目录；按 #49 的 f2f6df3 检出后先定向 7 passed，再全量通过。首次输出 /tmp/luna-recheck-provider-tests.log 保留。没有新增迁移，头仍 20261101_0021；清单 schema 未变。

本候选不包含 WP-3 管理查询、逐读法回执新字段、第二轨执行器、Wind 或新作者资产；开发分支同步 020a420 并保留其原有 expected_revision CAS。发版以本候选的精确 SHA 为准，不能钉开发分支 HEAD。线上 9319ae09 的暂停验收、采单及采集失败记录均未触碰。
