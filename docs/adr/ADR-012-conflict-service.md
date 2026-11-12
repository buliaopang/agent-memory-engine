# ADR-012：ConflictService 与 memory_conflicts
- 状态：已接受（2026-11-02，Q-C1~Q-C4）
- 决定：
  1. 新表 `memory_conflicts`（迁移 0005）：一条 active CONTRADICTS 最多一个 open 冲突；只允许 open→resolved；禁止删除；插入 open 冲突必须有端点一致的 active CONTRADICTS。
  2. 双方均为 active/conflicted 时开冲突；其中 active 的一方经 T5（system）变 conflicted——包括“已 conflicted 对新 active”（Q-C2）。candidate 参与只给复核提示。
  3. 数据库触发器：记忆进入 conflicted 必须有 open 冲突（从 archived 恢复时允许任一冲突记录）；不能以 conflicted 状态创建。直接 `change_status` 做 T5 因此被拒绝（行为变更，测试已更新）。
  4. Agent 可声明冲突；只有用户能解决，且必须给 reason 与非空 evidence（Q-C4）。解决方式：kept_both、dismissed（都保留 CONTRADICTS，Q-C3）、a_superseded_by_b、b_superseded_by_a（败方 T7/T8 + SUPERSEDES 边）。存在 open 冲突时 T6/T7 经 change_status 一律 409 CONFLICT_UNRESOLVED。
  5. 撤销 CONTRADICTS：冲突以 relation_revoked 关闭，不改状态；若一方仍 conflicted 且无 open 冲突，返回 `conflict_cleared_user_confirm_T6` 提示用户确认 T6（Q-C1）。
  6. 状态变更集中在 `LifecycleService`，MemoryService 与 ConflictService 共用。
- 0005 回填：对已有“active CONTRADICTS 且至少一方 conflicted”的数据补建 open 冲突（`cfl_backfill_<relation_id>`）。
