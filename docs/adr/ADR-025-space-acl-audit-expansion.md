# ADR-025：空间访问名单（ACL）与审计表扩容

- 状态：已采纳（2026-11-03，Phase 3a）
- 关联：ADR-013（审计）、ADR-018（空间）、ADR-020（检索先过滤后召回）

## 决定
1. 迁移 0009：SQLite 不能改 CHECK，审计表 `audit_events` 采用“复制重建”：建新表 → 按 seq 全量复制 →
   行数 + 全列 sha256 指纹比对 → 删旧表改名 → 重建 4 个索引与 2 个只追加触发器 → 再比对一次 → 记录触发器指纹。
   任一步失败即抛错，由 MigrationRunner 自动从迁移前备份恢复。新增事件类型 SPACE_CREATED、AGENT_REGISTERED、
   AGENT_DISABLED、AGENT_ENABLED、AGENT_CREDENTIAL_ISSUED、SPACE_MEMBER_GRANTED、SPACE_MEMBER_REVOKED；
   实体类型 space、agent。降级在存在新类型行时拒绝（审计不丢）。
2. 迁移 0010：`space_members` 增加 granted_at/by、revoked_at/by（旧行回填 granted_by='migration'），
   新增禁止删除触发器；`agents` 增加 credential_issued_at、disabled_at。撤销是打标记，不删行。
3. 规则：
   - 用户：全部可见可写。
   - 已启用的 Agent：所有 shared 空间可读可写；project/private 空间需显式授权：reader=只读，editor/owner=读写。
   - 看不见的记忆/空间一律 404（与不存在完全相同）；写入看不见的空间返回与“空间不存在”相同的 422 SPACE_NOT_FOUND。
   - 看得见但不能写 → 403 PERMISSION_DENIED。
   - Agent 不能建空间、建 Agent、发钥匙、授权（403）。
   - Agent 只能修改自己创建且仍是 candidate 的记忆（版本、元数据、分类、状态、归档/恢复）。
   - 建链/撤链：两端都可见，且对起点记忆所在空间有写权限。
4. 检索：请求的 scope_ids 先与“可读空间”求交集，再进入召回；graph_context、邻居遍历、graph、stats、
   列表里的对端 id 同样过滤。
5. 新增 check-graph G014：space_members 行必须引用存在的空间/Agent、有授权信息、撤销信息成对。

## 后果
- 访问判断每次请求实时读库，撤销/禁用立即生效。
- 审计查询（/memories/{id}/audit）对 Agent 仍返回该记忆的全部事件（不含正文与钥匙）。
