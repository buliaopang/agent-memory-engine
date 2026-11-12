# ADR-013：统一追加式审计 audit_events
- 状态：已接受（2026-11-02，Q-D1/Q-D2）
- 决定：
  1. 新表 `audit_events`（迁移 0005），触发器禁止 UPDATE/DELETE；`seq` 在 IMMEDIATE 写锁内分配，唯一、无间隙；与业务变更同事务写入，失败则一起回滚。
  2. 事件类型：MEMORY_CREATED / MEMORY_UPDATED / MEMORY_VERSION_CREATED / MEMORY_STATUS_CHANGED / MEMORY_RESTORED / EDGE_CREATED / EDGE_REVOKED / RELATION_CREATED / RELATION_REVOKED / CONFLICT_OPENED / CONFLICT_RESOLVED。
  3. before/after 只存状态与 content_hash，绝不存记忆正文（Q-D2）；代码层对 content/summary/excerpt 键直接报错。
  4. 同一用例的全部事件共用一个 correlation_id。
  5. 旧表 `memory_audit_events` 冻结为只读（禁止 INSERT，原 UPDATE/DELETE 禁止保持），永不删除（Q-D1）；旧行按原顺序回填到 audit_events（after 带 `legacy: true`）。
  6. T10 恢复目标从 audit_events 中最近一次“after.status=archived”事件的 before.status 读取。
- API 影响：`AuditEventRead` 改为新结构（event_id、seq、entity_type、before/after 等）；REST/MCP 尚未实现，契约仅在 API_SPEC 中描述。
