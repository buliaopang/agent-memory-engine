# ADR-029：反馈与衰减建议

- 状态：已采纳（2026-11-03，用户批准 PHASE_4_REVIEW.md 的 D1–D12；Phase 4）
- 关联：D5、D11、L6、ADR-013（审计）、ADR-025（ACL）

## 决定
1. 迁移 0013 新建 `memory_feedback`（feedback_id、memory_id FK、actor_type、actor_id、feedback ∈ useful/not_useful/outdated/wrong、reason、created_at），触发器禁止改、禁止删（append-only）。字段名沿用 MCP 契约（feedback、reason），不用计划书里的 kind/note（ADR-002 字段名一致）。
2. 审计事件新增 FEEDBACK_ADDED、MEMORY_PROMOTED、MEMORY_REJECTED、DISTILL_CREATED、EPOCH_CREATED、EPOCH_CLOSED、EPOCH_MEMBER_ADDED、EPOCH_MEMBER_REMOVED；实体类型新增 feedback、epoch、distill。做法同 0009：建新表、逐行复制、核对行数与全列 sha256、换名、重建触发器、记录指纹。MEMORY_REJECTED 是计划外新增的（D12 要求驳回也记来源列表）。
3. 用户和 Agent 都能对**看得见**的记忆反馈；看不见返回 404。只读 MCP 模式下不能反馈。
4. 防刷分：每个人（actor_id）只算最近一次。分值 useful +1、not_useful −1、outdated −1、wrong −2；score = 合计 / max(3, 人数)，限制在 [−1, 1]。至少按 3 人平均，一个人刷不满。反馈只影响排序和衰减建议，不改正文、不改状态。
5. 衰减：score = importance × 0.5^(天数 / 半衰期) + 0.2 × 反馈分，限制在 [0, 1]；天数从“最后更新或最后一次反馈”算起。默认半衰期 90 天（AME_DECAY_HALF_LIFE_DAYS）、门槛 0.15（AME_DECAY_THRESHOLD）。计划书写的是 e^(−Δt/半衰期)，实现用 0.5^，这样“半衰期”名副其实（90 天正好减半）。
6. 受保护、不进建议名单：importance ≥ 0.8（AME_DECAY_PROTECT_IMPORTANCE），或用户本人最近一次反馈是 useful（即“你确认过”）。
7. `GET /decay/suggestions`、`ame decay suggest` 只读，不写任何行（测试核对审计行数和文件哈希）。归档只能由用户一条一条做：`POST /decay/archive`、`ame decay archive <id>` 或 `ame archive --from-suggestion <id>`；执行时重新计算，已经不在建议里就返回 422。原因前加上 `decay suggestion score=…`，走现有 T9 归档（可恢复）。没有删除。

## 取舍
- 读取时计算，不存分数：2000 条数据 p95 约 28 ms（Linux），以后数据量大了再考虑缓存。
- 0013 的 downgrade 在有反馈行或新类型审计行时拒绝，回退靠迁移前备份（与 0003/0009 一致）。
