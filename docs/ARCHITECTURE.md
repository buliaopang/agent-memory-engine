# AME 总体架构

## 1. 逻辑分层

Agent Clients
→ Adapters（MCP / REST）
→ Application Core
→ Domain Services（Memory / DAG / Retrieval / Lifecycle / Access）
→ Infrastructure（SQLite / Vector Store / Embedding / Filesystem）

### Adapter
- REST Adapter：FastAPI 路由、请求/响应序列化、认证依赖。
- MCP Adapter：工具定义、参数校验、调用 Application Services。
- Adapter 不包含业务规则和 SQL。

### Application Core
统一编排用例：CreateMemory、GetMemory、SearchMemory、CreateEdge、CreateVersion、ArchiveMemory、BuildContext、ManageSpace。

### Domain
- Memory：节点、版本、来源、状态、层级。
- DAG：边类型、环检测、拓扑排序、邻居扩展、Epoch、Canonical Spine。
- Retrieval：候选召回、图扩展、排序、上下文预算。
- Lifecycle：晋升、归档、衰减、冲突。
- Access：Agent 身份、Space ACL、操作授权。

### Infrastructure
- SQLite：权威数据源。
- VectorStore：LanceDB 初始实现，接口可替换。
- EmbeddingProvider：可插拔。
- SourceStore：原始来源元数据与可选内容。
- Outbox Worker：向量索引同步、重试和死信处理。

## 2. 数据流

写入：
Agent → Adapter → AuthZ → Application Service → SQLite transaction（memory + edges + sources + outbox）→ Worker → Embedding → VectorStore。

检索：
Agent → Adapter → AuthZ / ACL scope filter → Vector candidates + lexical fallback → DAG expansion → rank → context packer → response。

## 3. 关键一致性规则
- SQLite commit 是记忆写入成功的判定点。
- 向量索引是最终一致，不是事务真相。
- 每个 outbox 事件具有唯一 event_id、状态、重试次数和错误信息。
- 索引可全量重建。
- 节点与边在同一数据库事务中写入。
- 每条边必须引用存在的节点。
- DAG 关系写入必须防环。
- 对同一记忆的版本更新采用乐观锁。

## 4. 图结构：Structural DAG + Semantic Relations（ADR-001）

图分为两层：
- **Structural DAG**（表 `memory_edges`，主键 `edge_id`）：有向、**有效边（status=active）**全局无环；所有结构边写入必须经 GraphService 环检测（跨全部结构边类型统一检测，revoked 边不参与），自环拒绝。边可撤销（status=revoked，保留原记录及撤销时间/操作者/原因），不可删除或改写；重建关系产生新 edge_id（ADR-006）。
- **Semantic Relations**（表 `memory_relations`，主键 `relation_id`，独立 RelationRepository / RelationService 与校验逻辑）：语义关系，**不执行 DAG 环检测**。类型限定 CONTRADICTS、SUPPORTS、RELATED_TO（ADR-007/008）。SUPPORTS 有向（from=证据 → to=被支持）；CONTRADICTS、RELATED_TO 无向，规范化存储为 `from_memory_id < to_memory_id`。同一类型同一端点对只允许一条 active 记录，重复或反向创建幂等返回已有关系，查询时两端均可见。语义关系同样可撤销（status + 撤销审计字段）。SIMILAR_TO 不在首版，留待向量阶段，且向量相似度不得写入人工声明的语义关系表。

Structural DAG 边方向（字段 `from_memory_id` → `to_memory_id`）：
- PARENT：父记忆 → 子记忆。
- REFERENCE：引用者 → 被引用节点。
- SUPERSEDES：新版本 → 旧版本。
- DERIVED_FROM：源记忆 → 派生记忆。

Semantic Relation 类型：
- CONTRADICTS：无向冲突关系。
- SUPPORTS：有向，证据 → 被支持（ADR-007 由结构边改为语义关系）。
- RELATED_TO：无向一般关联（ADR-008）。
以上均只存于 `memory_relations`，**禁止**作为 Structural DAG 边（数据库 CHECK 约束保证）。

SUPERSEDES 只用于“记忆 → 记忆”的取代（状态机 T7/T8），撤销只能经状态机 T11（ADR-006），同一记忆内的正文版本通过 `memory_versions.version` 递增表达，不创建边。

## 4.1 命名统一（ADR-002）
全项目（数据库列、REST、MCP、JSON Schema）统一：`memory_id`、`current_version`、`version_id`、`version`、`edge_id`、`relation_id`；结构边端点 `from_memory_id` / `to_memory_id`；来源记录 `source_id`。禁止同一概念多种字段名，禁止裸 `id` 列。

## 4.2 状态机（ADR-003）
六状态 candidate / active / conflicted / superseded / archived / rejected，合法转换、操作者、触发条件与审计见 docs/STATE_MACHINE.md。所有状态、分类、版本变化写入 append-only 审计表 `memory_audit_events`；历史版本 append-only（数据库触发器禁止 UPDATE/DELETE）。

## 4.3 Layer 与 Memory Type 正交（ADR-004）
Layer（L1–L4）与 memory_type（working/episodic/semantic/procedural）独立，任意组合合法。晋升可改 Layer；修改 memory_type 必须提供理由并审计。

## 4.4 字段规范（ADR-005）
tags → `memory_tags`；sources → `memory_sources`；parent_ids / reference_ids → `memory_edges`（PARENT / REFERENCE）；`metadata_json` 只存非核心扩展属性，核心字段名作为 metadata 键会被拒绝。

## 5. Canonical Memory Spine
主记忆链是检索优化视图，不是事实裁判。MVP 只定义接口和简单确定性排序，不把它作为写入正确性条件。排序考虑用户确认、来源、验证状态、重要性、时间。权重必须可配置并可解释。

## 6. Epoch
Epoch 是逻辑时间/项目阶段分组，不是数据库分区。MVP 支持创建和关联，后续实现自动切分。

## 7. 本地安全
- 默认监听 127.0.0.1。
- REST 使用本地随机 API Token；配置文件权限应限制为当前用户。
- MCP stdio 适配器从本地配置读取服务地址和令牌。
- CORS 默认关闭。
- 不暴露公网，不默认启动局域网监听。
- 日志不记录正文全文，除非用户显式开启诊断模式。

## 4.5 归档保护（ADR-009）
archived 记忆禁止 create_version，必须先 restore（T10，记录 actor、时间、原因）。Service 层校验 + SQLite 触发器（memory_versions 插入、memories.current_version 更新）双重保障，绕过 Service 直接写库同样失败。

## Phase 1.1 补充（2026-11-02，ADR-010~016）
- 应用层新增：`LifecycleService`（唯一的状态变更入口）、`ConflictService`、`application/audit.py`（审计写入助手）、`IntegrityService`（check-graph）。MemoryService 作为门面调用它们；未来 REST 与 MCP 适配器都只调用同一组 Service。
- 基础设施新增：`backup.py`（backup API）、`migration_runner.py`（备份→升级→后检查→失败自动恢复）、`integrity_repository.py`（只读 SQL）、`schema_guards.py`（各版本所需触发器清单）。
- 事务：UnitOfWork 写模式 `BEGIN IMMEDIATE`、读模式 `BEGIN`；每个写 UoW 一个 correlation_id；触发器 RAISE 在 UoW 退出时统一映射为领域错误。
- 防线分两层：Service 校验（友好错误）+ 数据库触发器（兜底，防直接写库/并发）。触发器被删除时由 `ame check-graph` G007/G008 发现。
- CLI：`ame`（check-graph / backup / restore / migrate），入口 `ame.cli:main`。
