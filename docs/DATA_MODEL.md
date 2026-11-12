# 数据模型与持久化规范

## 1. 核心表（ADR-001~005，迁移 0002_core_memory_tables 已实现）

命名规则见 ADR-002：主键使用 `<实体>_id`，禁止裸 `id`。

### memories（记忆头，不含正文）
- memory_id TEXT PRIMARY KEY
- memory_type TEXT NOT NULL CHECK（working/episodic/semantic/procedural）
- layer TEXT NOT NULL CHECK（L1/L2/L3/L4）— 与 memory_type 正交
- scope_type TEXT NOT NULL CHECK（private/project/shared）
- scope_id TEXT NOT NULL
- created_by TEXT NOT NULL
- created_at TEXT NOT NULL / updated_at TEXT NOT NULL（UTC ISO-8601）
- importance REAL NOT NULL CHECK 0..1
- confidence REAL NOT NULL CHECK 0..1
- status TEXT NOT NULL CHECK（candidate/active/conflicted/superseded/archived/rejected）
- current_version INTEGER NOT NULL CHECK ≥1（0003 触发器：archived 时不得变更）
- metadata_json TEXT NOT NULL — 仅非核心扩展属性

> 变更：正文 content / summary / content_hash 移至 memory_versions，避免新版本覆盖 memories 中的正文（“不得覆盖历史版本”）。当前正文 = memory_versions 中 version = current_version 的行。

### memory_versions（append-only，触发器禁止 UPDATE/DELETE；0003 起禁止为 archived 记忆插入，ADR-009）
- version_id TEXT PRIMARY KEY
- memory_id TEXT NOT NULL FK
- version INTEGER NOT NULL CHECK ≥1
- content TEXT NOT NULL
- summary TEXT NULL
- content_hash TEXT NOT NULL
- created_by TEXT NOT NULL
- created_at TEXT NOT NULL
- change_reason TEXT NULL（version=1 为空，之后必填）
- UNIQUE(memory_id, version)

### memory_edges（Structural DAG，环检测；迁移 0003 修订，ADR-006）
- edge_id TEXT PRIMARY KEY
- from_memory_id TEXT NOT NULL FK
- to_memory_id TEXT NOT NULL FK
- edge_type TEXT NOT NULL CHECK（PARENT/REFERENCE/SUPERSEDES/DERIVED_FROM）— SUPPORTS 已移出（ADR-007）
- status TEXT NOT NULL CHECK（active/revoked）
- created_by、created_at、metadata_json
- revoked_at、revoked_by、revoked_actor_type、revoked_reason（TEXT NULL；status=revoked 时必须全部非空，active 时必须全部为空 — CHECK）
- CHECK(from_memory_id <> to_memory_id)
- 部分唯一索引 uq_memory_edges_active_triple：UNIQUE(from_memory_id, to_memory_id, edge_type) WHERE status='active'（取代 0002 的全表三元组唯一约束，允许历史 revoked 记录与新 active 记录并存）
- 触发器：禁止 DELETE；UPDATE 只允许 active→revoked 且不得修改端点、类型、创建信息
- 环检测与 parent_ids / reference_ids 只看 status='active'

### memory_relations（Semantic Relations，不做环检测；迁移 0003 修订，ADR-007/008）
- relation_id TEXT PRIMARY KEY
- relation_type TEXT NOT NULL CHECK（CONTRADICTS/SUPPORTS/RELATED_TO）
- from_memory_id TEXT NOT NULL FK / to_memory_id TEXT NOT NULL FK（0002 的 low_memory_id/high_memory_id 已改名，ADR-002）
- status TEXT NOT NULL CHECK（active/revoked）
- created_by、created_at、metadata_json
- revoked_at、revoked_by、revoked_actor_type、revoked_reason（同 memory_edges 规则）
- CHECK(from_memory_id <> to_memory_id)
- CHECK(relation_type = 'SUPPORTS' OR from_memory_id < to_memory_id) — 无向类型规范化
- 部分唯一索引 uq_memory_relations_active_pair：UNIQUE(relation_type, from_memory_id, to_memory_id) WHERE status='active'
- 触发器：禁止 DELETE；UPDATE 只允许 active→revoked

### memory_tags
- memory_id TEXT FK、tag TEXT；PRIMARY KEY(memory_id, tag)；INDEX(tag)

### memory_sources
- source_id TEXT PRIMARY KEY
- memory_id TEXT NOT NULL FK
- source_type TEXT NOT NULL CHECK（conversation/file/task/manual/tool）
- source_ref、excerpt、content_hash（NULL 可）
- created_at、metadata_json

### memory_audit_events（append-only 审计）
- audit_id TEXT PRIMARY KEY
- memory_id TEXT NOT NULL FK
- event_type TEXT NOT NULL CHECK（created/status_changed/classification_changed/version_created）
- from_value TEXT NULL / to_value TEXT NOT NULL
- trigger TEXT NOT NULL（状态机规则编号 T1–T12，或 version / classification）
- actor_type TEXT NOT NULL CHECK（user/agent/system）/ actor_id TEXT NOT NULL
- reason TEXT NOT NULL
- created_at TEXT NOT NULL

### 以下表属于后续阶段，尚未建表（列名已按 ADR-002 修订）
### agents
- agent_id TEXT PRIMARY KEY
- display_name TEXT NOT NULL
- agent_type TEXT NOT NULL
- credential_hash TEXT NULL
- enabled INTEGER NOT NULL
- created_at TEXT NOT NULL

### spaces
- space_id TEXT PRIMARY KEY
- name TEXT NOT NULL
- scope_type TEXT NOT NULL
- owner_agent_id TEXT NULL
- created_at TEXT NOT NULL

### space_members
- space_id TEXT NOT NULL
- agent_id TEXT NOT NULL
- role TEXT NOT NULL（owner/editor/reader）
- PRIMARY KEY(space_id, agent_id)

### outbox_events
- event_id TEXT PRIMARY KEY
- event_type TEXT NOT NULL
- aggregate_id TEXT NOT NULL
- payload_json TEXT NOT NULL
- status TEXT NOT NULL（pending/processing/done/failed）
- attempts INTEGER NOT NULL DEFAULT 0
- next_attempt_at TEXT NULL
- last_error TEXT NULL
- created_at TEXT NOT NULL
- processed_at TEXT NULL

### epochs
- epoch_id TEXT PRIMARY KEY
- name TEXT NOT NULL
- project_space_id TEXT NULL
- start_at TEXT NOT NULL
- end_at TEXT NULL
- metadata_json TEXT NOT NULL

### epoch_members
- epoch_id TEXT NOT NULL
- memory_id TEXT NOT NULL
- PRIMARY KEY(epoch_id, memory_id)

## 2. 索引
- memories(scope_type, scope_id, status, layer)；memories(created_by, created_at)；memories(memory_type)
- memory_edges(from_memory_id, edge_type, status)；memory_edges(to_memory_id, edge_type, status)；uq_memory_edges_active_triple（部分唯一）
- memory_relations(from_memory_id, status)；memory_relations(to_memory_id, status)；uq_memory_relations_active_pair（部分唯一）
- memory_versions UNIQUE(memory_id, version)
- memory_tags(tag)；memory_sources(memory_id)；memory_audit_events(memory_id, created_at)
- outbox_events(status, next_attempt_at)（后续阶段）

## 3. 约束
- 重要性、置信度范围 0..1。
- 外键启用。
- 删除采用软删除/归档；默认禁止物理级联删除。
- 内容版本不可原地覆盖：memory_versions 与 memory_audit_events 由 SQLite 触发器禁止 UPDATE/DELETE。
- metadata_json 不得包含核心字段（tags、sources、parent_ids、reference_ids、status、layer 等），由 DTO 校验拒绝。
- 所有写入记录 UTC 时间。
- content_hash 用于去重提示，不可单独据此合并语义相近但不同的记忆。

## 4. 向量索引
向量记录至少包含：
- memory_id
- embedding_model
- embedding_dimension
- vector
- content_hash
- version（被索引的版本序号）
- indexed_at

VectorStore 必须实现 upsert、delete/tombstone、search、health、rebuild。SQLite 保存索引状态和模型信息。向量库数据可从有效 memories 重建。

## 5. 迁移历史
- 0001_baseline：ame_meta。
- 0002_core_memory_tables：核心表（不修改）。
- 0003_link_revocation：重建 memory_edges / memory_relations（status、撤销字段、部分唯一索引、不可删改触发器）；SUPPORTS 结构边迁入 memory_relations（relation_id = 'rel_' || edge_id）；low/high 改名 from/to；archived 版本保护触发器。不可逆（downgrade 报错）。

## Phase 1.1 新增（迁移 0004 / 0005）
### 0004_dag_integrity
- 部分索引 `ix_memory_edges_active_adj (from_memory_id, to_memory_id) WHERE status='active'`。
- 触发器：`trg_memory_edges_no_cycle_insert`、`trg_memory_edges_no_cycle_update`、`trg_memory_edges_endpoints_insert`、`trg_memory_relations_endpoints_insert`。
- `ame_meta.trigger_fingerprints`：全部触发器 SQL（空白归一化）sha256。
### 0005_conflicts_audit
- `memory_conflicts`：conflict_id PK、relation_id FK、memory_a_id < memory_b_id、status open|resolved、opened_at/by/actor_type、evidence_json、resolved_at/by/actor_type、resolution、resolution_reason、resolution_evidence_json。部分唯一索引：每个 relation 至多一个 open。
- `audit_events`：event_id PK、seq UNIQUE、event_type、entity_type(memory|version|edge|relation|conflict)、entity_id、memory_id、related_memory_id（无外键，审计独立于实体）、actor_type、actor_id、occurred_at、reason、before_json、after_json、correlation_id。
- `memory_audit_events`：冻结（只读）。
- 触发器：audit_events 禁改禁删；旧审计表禁插入；冲突禁删、只能 open→resolved、open 冲突需 active CONTRADICTS；conflicted 需冲突证据；不得以 conflicted 创建；archived/rejected 端点禁新增边/关系。

## Phase 1.2 更新（2026-11-03）
- agents / spaces / space_members 已由迁移 0006 建表（上文“尚未建表”的说明对这三张表不再适用，原文保留）；outbox_events、epochs、epoch_members 仍未建。
- 新约束：memories.(scope_id, scope_type) 必须对应 spaces 中同类型的空间（触发器）；spaces/agents 不可删除。见 ADR-018。

## Phase 2 补充（迁移 0007_search_outbox，2026-11-03）
- `memory_fts`：FTS5 trigram 虚表，列为 memory_id、version（不索引）以及 content、summary、tags，只放当前版本。由触发器同步，可重建，G012 检查一致性。
- `outbox_events`：列为 event_id、event_type（memory.index、memory.status、memory.reindex）、aggregate_id、payload_json（只含 id 和版本）、status（pending、processing、done、failed）、attempts、next_attempt_at（兼作租约）、last_error、created_at、processed_at。禁止删除。
- `vector_index_state`：列为 memory_id（PK/FK）、status（indexed、failed、removed、disabled）、indexed_version、embedding_model、embedding_dimension、content_hash、indexed_at、last_error、updated_at。
- 新增触发器：trg_memory_versions_fts_sync、trg_memory_tags_fts_insert、trg_memory_tags_fts_delete、trg_memory_versions_outbox、trg_memories_status_outbox、trg_outbox_events_no_delete（指纹记录在 ame_meta）。

## Phase 4 补充（迁移 0012–0015，2026-11-03）
- 0012：索引 ix_agents_credential_hash。
- 0013：`memory_feedback`（feedback_id PK、memory_id FK、actor_type、actor_id、feedback CHECK、reason、created_at），索引 ix_memory_feedback_memory_actor，触发器禁改禁删；audit_events 重建，事件/实体类型扩充（见 ADR-029）。
- 0014：`epochs`、`epoch_members`（上文“epochs、epoch_members 仍未建”的说明不再适用，原文保留）；触发器禁删、关闭后不可改、关闭后不可加入/移出成员。
- 0015：`distill_jobs`（唯一 (rule, input_key)，output_memory_id FK），禁改禁删。
- check-graph：G015（反馈/Epoch 行）、G016（提炼输出）。
