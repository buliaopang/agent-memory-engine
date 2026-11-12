# ADR-032：提炼草稿与晋升

- 状态：已采纳（2026-11-03，用户批准 PHASE_4_REVIEW.md 的 D1–D12；Phase 4）
- 关联：D2、D3、D4、D12、ADR-003（状态机）

## 决定
1. 只能由用户手动触发：`POST /api/v1/distill`、`ame distill run --scope <空间> [--tag] [--threshold] --reason`。没有定时任务，不用大模型。
2. 规则 `tag_bigram_v1`：同一空间、同一标签的 active 记忆，按中文字符二元组的 Jaccard 重合度 ≥ 阈值（默认 0.5，AME_DISTILL_THRESHOLD）用并查集分组，结果确定（与输入顺序无关）。一组最多 20 条；已经是草稿（标签 distill-draft）的记忆不再参与。
3. 每组生成一条新记忆：状态 candidate，创建者 `system:distill`，正文是原文摘录（每条最多 200 字，不改写），标签 [原标签, distill-draft]；每个来源写一条 memory_sources（type=tool、source_ref=`ame://memory/<id>`、原文哈希），并从每条原记忆连一条 DERIVED_FROM 边到草稿。原记忆的正文、版本、状态都不动（测试核对指纹）。
4. 迁移 0015 新建 `distill_jobs`（job_id、rule、params_json、input_ids_json、input_key、output_memory_id、status、actor、created_at），禁止改和删；(rule, input_key) 唯一，同样的输入再跑一次不会重复生成。审计写 DISTILL_CREATED（含来源列表和阈值）。
5. 审核：`GET /review`、`ame review list` 列出 candidate（标出哪些是提炼草稿及其来源）。`POST /memories/{id}/promote|reject`、`ame review promote|reject` 只有用户能做（Agent 403），只对 candidate 有效；走现有 T3/T4 转换，并另写 MEMORY_PROMOTED / MEMORY_REJECTED 审计：操作人、时间、原因、来源列表、前后状态（D12）。
6. check-graph 新增 G016：提炼输出必须存在、有来自每个来源的 DERIVED_FROM 边；不是 candidate 时必须有用户的晋升/驳回/状态变更审计。G015 检查反馈和 Epoch 行的引用与关闭规则。

## 取舍
- 二元组规则对改写较大的内容分不到一组；要接本地模型需要新 ADR。
- 草稿正文可能带着原文里的“指令”：草稿永远是 candidate，MCP 输出仍按 ADR-028 包裹。
