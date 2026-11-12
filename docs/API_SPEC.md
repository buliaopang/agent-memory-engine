# REST API v1 规范

Base URL：`http://127.0.0.1:8765/api/v1`

认证：`Authorization: Bearer <local-token>`。健康检查可配置为无需认证，但不得返回敏感信息。

统一错误：
```json
{
  "error": {
    "code": "VERSION_CONFLICT",
    "message": "Expected version does not match current version",
    "request_id": "req_xxx",
    "details": {}
  }
}
```

## Endpoints

### POST /memories
创建记忆。支持 `Idempotency-Key` header。
请求字段：content、summary、memory_type、layer、scope_type、scope_id、importance、confidence、tags、parent_ids、reference_ids、sources、metadata（DTO：MemoryCreate）。metadata 不得包含核心字段。
初始状态：用户手动录入为 active（T2），Agent 写入为 candidate（T1）。
响应：MemoryRead（见 schemas/memory.schema.json，含 memory_id、current_version、version_id、status 等）；Phase 2 起追加 vector_status。

### GET /memories/{memory_id}
获取记忆。必须执行空间权限校验。支持 include_sources/include_edges。

### PATCH /memories/{memory_id}
只允许更新可变元数据，例如 importance、tags。不得直接覆盖正文；status 不经 PATCH 修改。

### POST /memories/{memory_id}/status
状态转换（DTO：StatusChange：to_status、reason（必填）、superseded_by_memory_id）。必须符合 docs/STATE_MACHINE.md；非法转换 409 INVALID_STATUS_TRANSITION，操作者无权 403 TRANSITION_NOT_PERMITTED。

### POST /memories/{memory_id}/classification
修改 layer / memory_type（DTO：ClassificationChange）。修改 memory_type 必须带 reason，否则 422 REASON_REQUIRED。

### POST /memories/{memory_id}/versions
创建新正文版本。请求包含 content、summary、change_reason、expected_version。archived 记忆返回 409 MEMORY_ARCHIVED，必须先 restore（ADR-009）。成功后新版本（version = expected_version + 1）成为 current_version，旧版本保留且不可修改；不创建 SUPERSEDES 边（SUPERSEDES 仅用于记忆之间的取代）。响应：VersionRead（version_id、memory_id、version …）。

### GET /memories/{memory_id}/versions
列出历史版本。

### POST /memories/search
请求：
```json
{
  "query": "此前验证过的桌面自动化流程",
  "memory_types": ["procedural"],
  "scope_ids": ["shared", "project:capcut"],
  "layers": ["L2", "L3", "L4"],
  "top_k": 10,
  "include_graph_context": true,
  "include_sources": true,
  "include_conflicts": false
}
```
响应包含 results、score_breakdown、graph_context、sources、query_id。

### POST /edges
创建 Structural DAG 边。请求 from_memory_id、to_memory_id、edge_type（PARENT/REFERENCE/SUPERSEDES/DERIVED_FROM）、metadata。验证节点存在、权限和环检测（仅 active 边参与；409 DAG_CYCLE_DETECTED）。存在相同 active 边时幂等返回已有 edge_id（created=false）。响应 EdgeRead（含 status、revoked_*）。

### POST /edges/{edge_id}/revoke
请求 reason（必填）。将边标记 revoked，记录 revoked_at / revoked_by / revoked_actor_type / revoked_reason，原记录保留。SUPERSEDES 边不可直接撤销，只能经状态转换 T11（superseded→active）。已撤销再撤销 409。

### GET /memories/{memory_id}/edges
参数 include_revoked（默认 false）。历史查询可返回已撤销边。

### POST /relations
创建 Semantic Relation。请求 from_memory_id、to_memory_id、relation_type（CONTRADICTS / SUPPORTS / RELATED_TO）、metadata。不做环检测。SUPPORTS 有向（from=证据）；CONTRADICTS / RELATED_TO 无向规范化（from<to），重复/反向创建返回已有 active relation_id。响应 RelationRead（含 directed、status、review_notice）。仅 CONTRADICTS 会触发状态机 T5。

### POST /relations/{relation_id}/revoke
请求 reason（必填）。标记 revoked 并记录撤销审计字段；之后可重新创建（新 relation_id）。

### GET /memories/{memory_id}/relations
参数 include_revoked（默认 false）。两端均可查到同一关系。

### GET /memories/{id}/neighbors
参数：edge_types、relation_types、direction、depth、limit。

### GET /graph
参数：space_id、center_id、depth、limit。必须限制最大节点数，避免一次返回全图。

### POST /memories/{id}/archive
归档，不物理删除。

### POST /memories/{id}/restore
恢复归档记忆，需权限。

### GET /memories/{memory_id}/audit
读取审计事件（AuditEventRead）。

### GET /agents
列出当前调用者可见的 Agent。

### POST /agents
管理员注册 Agent。凭据只返回一次，数据库仅保存哈希。

### GET /spaces / POST /spaces
列出/创建空间。

### GET /health
返回服务、SQLite、VectorStore、Worker 状态。

### GET /stats
返回记忆数量、层级分布、空间分布、索引队列状态等，不泄漏正文。

## 搜索排序
先执行 ACL 过滤，再召回。默认排除 conflicted（include_conflicts=true 时返回并标注）。默认向量权重 0.35、图关系 0.20、重要性 0.15、置信度 0.20、新鲜度 0.10。无 embedding 时启用关键词 + 图检索降级，并在响应标明 retrieval_mode。

## 幂等与并发
- 创建/写入使用 Idempotency-Key。
- 更新使用 expected_version。
- 不匹配返回 HTTP 409。
- 不允许无声覆盖。

## 字段命名（ADR-002）
所有请求/响应字段与数据库列、MCP 参数同名：memory_id、current_version、version_id、version、edge_id、relation_id、from_memory_id、to_memory_id、source_id。权限不足访问私有记忆一律返回 404（D-Q1）。

## 实现状态
DTO（src/ame/application/schemas.py）与 Application Service 已实现；REST 路由除 /health 外尚未实现（Phase 1 后续 / Phase 3 结合认证实现）。

## Q11–Q14 修订说明
本文件新增/修改的接口仍只是契约；REST 业务路由未实现。

## Phase 1.1：未来 Service 层契约（REST 未实现，仅设计）
- `MemoryService.resolve_conflict(conflict_id, ConflictResolve{resolution, reason, evidence}, actor)` → ConflictRead；仅 user；evidence 非空。
- `MemoryService.list_conflicts(memory_id, include_resolved=False)` → list[ConflictRead]。
- `revoke_relation` 对 CONTRADICTS 返回的 `review_notice` 可能为 `conflict_cleared_user_confirm_T6`。
- `create_relation(CONTRADICTS)` 的 `review_notice`：`both_marked_conflicted` / `conflict_opened` / `candidate_contradicts_active_review_required`。
- `list_audit` 返回新 AuditEventRead（event_id、seq、event_type、entity_type、entity_id、memory_id、related_memory_id、actor_type、actor_id、occurred_at、reason、before、after、correlation_id）。
- 新错误码（409）：CONFLICT_UNRESOLVED、CONFLICT_EVIDENCE_REQUIRED、LINK_ENDPOINT_FROZEN；DB 繁忙映射为 StorageError（500）。

## Phase 1.2 实现状态（2026-11-03，ADR-017）
- 已实现（Linux 实测）：POST/GET/PATCH /memories；/memories/{id}/status、/classification、/archive、/restore；/memories/{id}/versions（POST/GET）；/edges、/edges/{id}/revoke、/memories/{id}/edges；/relations、/relations/{id}/revoke、/memories/{id}/relations；/memories/{id}/audit；/memories/{id}/conflicts；POST /conflicts/{conflict_id}/resolve（新增，裁决冲突，仅用户）。
- 认证：`Authorization: Bearer <data_dir/api_token 内容>`；/health 免认证。401 UNAUTHORIZED。
- Idempotency-Key：暂不支持，返回 501 IDEMPOTENCY_NOT_SUPPORTED（Phase 3）。
- PATCH 请求：{importance?, tags?, reason（必填）}。archive/restore 请求：{reason}。
- 新错误码：SPACE_NOT_FOUND（422）、UNAUTHORIZED（401）、IDEMPOTENCY_NOT_SUPPORTED（501）、ALREADY_EXISTS（409）、METHOD_NOT_ALLOWED（405）。
- 未实现：/memories/search、/memories/{id}/neighbors、/graph、/agents、/spaces。
- Windows：未验证。

## Phase 2 已实现（2026-11-03，ADR-020/021）
所有接口都需要 Bearer 令牌；校验错误返回 422，且不回显提交的值。
- `POST /api/v1/memories/search`
  - 请求体：`{query(1..2000), memory_types[], scope_ids[], layers[], top_k(1..100, 默认 10), include_graph_context, include_sources(默认 true), include_conflicts}`
  - 返回：`{retrieval_mode: hybrid|lexical_graph, degraded_reason, results:[{memory, score, score_breakdown{vector|null, keyword, graph, importance, confidence, freshness, weights, vector_used}, graph_context[]}]}`
  - JSON Schema：schemas/search-request.schema.json、schemas/search-response.schema.json
- `GET /api/v1/memories/{id}/neighbors?direction=in|out|both&depth=1..3&limit=1..200&edge_types=A,B&relation_types=C`
  - 返回 `{center_id, nodes[{memory_id, memory_type, layer, scope_id, status, summary, depth}], links[], truncated}`
- `GET /api/v1/graph?space_id=…|center_id=…&depth&limit(≤500)`：space_id 和 center_id 必须给一个。
- `GET /api/v1/stats`：返回 `{memories{按状态计数, total}, outbox{}, vector_state{}, retrieval_mode}`
- `GET /api/v1/health`：worker 字段为 `{status: running|stopped|disabled, pending_events, failed_events, last_error}`，vector_store 字段为 `{status, retrieval_mode}`
- MemoryRead 新增 `vector_status` 字段。
- BuildContext 只在服务层实现（ContextService），没有对应的 REST 接口；MCP memory_get_context 在 Phase 3。

## Phase 3a 已实现（2026-11-03，ADR-024/025）
- 认证：`Authorization: Bearer <用户令牌 | Agent 钥匙>`；无效或 Agent 已禁用 → 401 UNAUTHORIZED。
- 所有读取接口按调用者过滤：看不见 → 404 NOT_FOUND；可见不可写 → 403 PERMISSION_DENIED。
- 新接口（写操作仅用户，Agent 调用 → 403）：
  - `GET /api/v1/spaces`（只列可读空间）、`POST /api/v1/spaces`、`GET /api/v1/spaces/{id}`
  - `GET|POST /api/v1/spaces/{id}/members`（body: agent_id, role=reader|editor|owner, reason）
  - `POST /api/v1/spaces/{id}/members/{agent_id}/revoke`（body: reason）
  - `GET /api/v1/agents`（Agent 只看到自己）、`POST /api/v1/agents`
  - `POST /api/v1/agents/{id}/credentials`（body: reason；返回 api_key，仅此一次）
  - `POST /api/v1/agents/{id}/disable`、`/enable`（body: reason）

## Phase 3b 已实现（2026-11-03，ADR-027）
- 请求头 `Idempotency-Key`（1–255 个可见 ASCII 字符）用于 POST/PUT/PATCH/DELETE；GET/HEAD 忽略。
- 同调用者 + 同键 + 同请求：返回第一次的状态码与响应体，响应头 `Idempotency-Replayed: true`。
- 同键不同请求：422 `IDEMPOTENCY_KEY_MISMATCH`；第一次尚未完成：409 `IDEMPOTENCY_IN_PROGRESS`；键格式错误：422 `VALIDATION_ERROR`。
- 记录保存 24 小时（`AME_IDEMPOTENCY_TTL_HOURS`）。原 501 `IDEMPOTENCY_NOT_SUPPORTED` 不再返回。

## Phase 3c 已实现（2026-11-03，ADR-028）
- `POST /api/v1/context`：请求体为 ContextRequest `{task_description(1..4000), scope_ids[], layers[], memory_types[], token_budget(50..32000, 默认 2000), include_procedural, include_sources}`；返回 ContextPack `{notice, retrieval_mode, token_budget, used_tokens, items[{memory_id, memory_type, layer, status, score, text, truncated, tokens, source_ids}], omitted}`。只读，ACL 与搜索相同。
- `User-Agent: ame-mcp/<ver>` 的请求：MCP 关闭时返回 403 `MCP_DISABLED`；只读模式下，除 GET、`/memories/search`、`/context` 外返回 403 `MCP_DISABLED`。
- MCP 工具输出格式：成功为 `{ok:true, notice, trust:"untrusted_data", data, idempotency_replayed?}`，失败为 `{ok:false, error:{code, message, http_status, request_id}}`；服务未运行时 code 为 `AME_UNAVAILABLE`，http_status 为 0。

## Phase 4 已实现（2026-11-03，ADR-029–032）
- `POST /api/v1/memories/{id}/feedback` body `{feedback: useful|not_useful|outdated|wrong, reason}` → 201 FeedbackRead；`GET` 同路径 → `{memory_id, counts, voters, score, items[]}`。看不见 404。
- `GET /api/v1/epochs`、`POST /api/v1/epochs` `{name, space_id?, start_at?, reason, metadata?}` → 201；`GET /epochs/{id}`；`POST /epochs/{id}/close` `{reason}`；`POST /epochs/{id}/members` 与 `/members/remove` `{memory_id, reason}`。写操作仅用户（403）；已关闭 409 EPOCH_CLOSED。
- `POST /api/v1/memories/search` 新增可选 `epoch_id`（不存在或看不见 404）。
- `GET /api/v1/spine?scope_id=&epoch_id=&limit=` → `{total_candidates, items[{memory_id, status, scope_id, summary, score, components, weights, explanation[]}]}`。
- `GET /api/v1/decay/suggestions?scope_id=&threshold=&limit=` → `{threshold, half_life_days, suggestions[...]}`（只读）；`POST /api/v1/decay/archive` `{memory_id, reason}` 仅用户，不在建议里 422。
- `POST /api/v1/distill` `{scope_id, tag?, threshold?, reason}` → 201 `{created[{job_id, memory_id, rule, source_memory_ids}], skipped_existing, groups_considered}`，仅用户。
- `GET /api/v1/review?scope_id=`；`POST /api/v1/memories/{id}/promote|reject` `{reason}`，仅用户，仅 candidate。
- 审计 `AuditEventRead` 新增 `related_hidden: bool`（L3）。所有新 POST 支持 Idempotency-Key。
