# 2026-11-02 #49 重新核对采单修复候选

基线 fb88c486（#49 仍在使用）。补齐本人令牌 POST /api/internal/v2/user-data/resolutions/{id}/refresh，复用服务端重解析；用户及应用隔离，已选资料不动，无变化不新增修订，不确认、不授权、不采集。

定向含真实 knowbench 客户端 → HTTP → knowbase → 隔离 PostgreSQL：34 passed。未触碰线上任务；用户暂停验收状态不变，正式发布交 qwen。

发布基线全仓静态检查暴露既有未定义导入/端口不一致/交付边界问题。本分支移植开发分支 5bfb60f 的修复，解决冲突时剔除其父提交 WP-3 public_holdings 功能，不带入第二轨新接口。全量验证结果待下节记录。
