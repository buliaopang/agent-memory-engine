# ADR-030：Epoch（阶段）

- 状态：已采纳（2026-11-03，用户批准 PHASE_4_REVIEW.md 的 D1–D12；Phase 4）
- 关联：D11、DATA_MODEL 原设计

## 决定
1. 迁移 0014 新建 `epochs`（epoch_id、name、space_id 可空 FK、start_at、end_at、status open|closed、created_by、created_at、metadata_json）和 `epoch_members`（主键 (epoch_id, memory_id)、added_at/added_by、removed_at/removed_by）。
2. 只有用户能创建、关闭、加入、移出（Agent 调用返回 403）；Agent 只能读自己看得见的 Epoch，成员列表也按可见性过滤。绑定了空间的 Epoch，看不见该空间时返回 404。
3. 数据库触发器：epochs 和 epoch_members 禁止删除；已关闭的 Epoch 不能再改（不能重新打开），也不能再加入或移出成员（报 EPOCH_CLOSED，HTTP 409）。
4. 移出是软删除（写 removed_at/removed_by），再加入会恢复同一行；重复加入是幂等的，不写审计。Epoch 绑定了空间时，只能加入该空间的记忆（422）。
5. 搜索支持 `epoch_id` 过滤（只看该 Epoch 当前成员）；`/spine?epoch_id=` 同样支持。名称支持中文。
6. 每次变更都写审计（EPOCH_CREATED / EPOCH_CLOSED / EPOCH_MEMBER_ADDED / EPOCH_MEMBER_REMOVED）。

## 取舍
- 不支持改名和重新打开；需要时新建一个 Epoch。
- 0014 的 downgrade 在有任何 Epoch 时拒绝。
