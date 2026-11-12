# ADR-018：Agent / Space 数据表（仅表与基础增查）

- 状态：已采纳（2026-11-03，用户批准 D2-A）
- 迁移：0006_agents_spaces（增量；0001–0005 字节未变，测试校验 SHA-256）

## 决定
1. 新建 agents、spaces、space_members（列名同 DATA_MODEL）。
2. 回填：每个已有的 memories.scope_id 生成一个 space（scope_type 取自记忆，name=scope_id，owner 为空，created_at=该空间最早记忆的时间）。不修改任何记忆行。
3. 若同一 scope_id 被多个 scope_type 使用，迁移中止，迁移器自动恢复迁移前备份（保守，人工处理）。
4. 预置默认空间 `shared`（scope_type=shared）。
5. 数据库触发器：记忆的 (scope_id, scope_type) 必须对应已存在且类型相同的空间（插入和修改都检查，SPACE_NOT_FOUND）；spaces/agents 不可删除；space_id/scope_type 不可修改。check-graph G007/G008 覆盖新触发器。
6. SpaceService / AgentService：只允许用户创建；Agent 注册不发凭据（credential_hash 为空）。另提供本地命令 `ame space create|list`（因 /spaces 接口推迟到 Phase 3，否则无法建新空间）。
7. 不做：权限检查（谁能读写哪个空间）、space_members 的使用、/agents /spaces 接口——Phase 3。

## 已知限制
- 创建空间/注册 Agent 暂不写 audit_events：0005 中 audit_events.entity_type 的 CHECK 只允许 memory/version/edge/relation/conflict，扩展需重建表，留待 Phase 3 一并设计（ADR 后续追加）。行内保留 created_at。
