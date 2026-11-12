# ADR-023：中文“去空格副本”索引（不引入分词库）

- 状态：已采纳（2026-11-03，用户批准 SEGMENTATION_PLAN.md 全部推荐项）
- 关联：ADR-020（FTS5 trigram + LIKE 兜底）；部分取代 DECISIONS C18 的限制

## 背景
trigram 索引对空格敏感：“部署流程”搜不到写成“部署 流程”的记忆。

## 决定
1. 迁移 0008 新增 FTS5 trigram 虚拟表 `memory_fts_compact`（memory_id、version 不索引；content、summary），
   存当前版本正文/摘要去掉 半角空格、全角空格 U+3000、TAB、LF、CR 之后的文字。标点保留。标签不进副本。
2. 一个触发器 `trg_memory_versions_fts_compact_sync` 在新当前版本写入时同步；0007 的触发器不动。
3. 查询：每个查询词同时查 `memory_fts` 与 `memory_fts_compact`（词先去空格），结果取并集；打分规则不变。
   副本表不存在（例如降级到 0007）时自动只查原索引。
4. `ame index rebuild-lexical` 同一事务内重建两张表；check-graph 新增 G013（副本与当前版本一致）；G008 新增探测。
5. 不引入 jieba 等分词依赖；以后如需要，作为可选 extra 另立 ADR。
6. 0008 的 downgrade 只删除派生表和触发器，不触碰源数据。

## 后果
- 中文空格/换行差异不再影响命中；英文结果只会变多（如 "desktopautomation" 能命中 "Desktop Automation"）。
- 去空格后可能跨句误命中；标点分隔的（“部署，流程”）仍不命中。
- 不是真正的分词：“部署的流程”仍不命中“部署流程”。
- FTS5 影子表判定改为精确后缀（_data/_idx/_content/_docsize/_config），避免把 memory_fts_compact 误当成 memory_fts 的影子表。
