# MCP 接口规范

MCP Server 是 REST 之外的另一适配器，必须调用相同 Application Services。

首版工具（契约以 schemas/mcp_tools.json 为准，字段名与 REST DTO 一致，ADR-002）：
1. memory_write
2. memory_read
3. memory_search
4. memory_create_version
5. memory_link
6. memory_neighbors
7. memory_get_context
8. memory_archive
9. memory_feedback
10. memory_list_spaces
11. memory_get_stats
12. memory_relate
13. memory_change_status
14. memory_change_classification
15. memory_revoke_edge
16. memory_revoke_relation

## memory_write
参数：content、summary、memory_type、layer、scope_type、scope_id、importance、confidence、parent_ids、reference_ids、sources、tags、metadata、idempotency_key。（`source` 单数已改为 `sources`，与 REST 一致）

## memory_read
参数：memory_id、include_sources、include_edges、include_relations。

## memory_search
参数：query、scope_ids、layers、memory_types、top_k、include_graph_context、include_sources、include_conflicts。

## memory_create_version
参数：memory_id、content、summary、change_reason、expected_version。

## memory_link
参数：from_memory_id、to_memory_id、edge_type（PARENT/REFERENCE/SUPERSEDES/DERIVED_FROM）、metadata。只创建 Structural DAG 边，执行环检测；不接受 CONTRADICTS / SUPPORTS / RELATED_TO。

## memory_relate（新增）
参数：from_memory_id、to_memory_id、relation_type（CONTRADICTS/SUPPORTS/RELATED_TO）、metadata。创建 Semantic Relation，不做环检测。

## memory_revoke_edge（新增，ADR-006）
参数：edge_id、reason。SUPERSEDES 边不可用此工具撤销（走 memory_change_status T11）。

## memory_revoke_relation（新增，ADR-008）
参数：relation_id、reason。

## memory_change_status（新增）
参数：memory_id、to_status、reason、superseded_by_memory_id。遵循 STATE_MACHINE.md。

## memory_change_classification（新增）
参数：memory_id、layer、memory_type、reason（改 memory_type 必填）。

## memory_neighbors
参数：memory_id、edge_types、relation_types、direction、depth、limit、include_revoked。

## memory_get_context
参数：task_description、scope_ids、layers、memory_types、token_budget、include_procedural、include_sources。
返回可直接注入 Agent 上下文的精简内容，同时保留 memory_id 和来源。

## MCP 传输
- 首版支持 stdio。
- 后续支持 Streamable HTTP。
- stdio 进程作为客户端调用运行中的本地 REST 服务（D-Q4），不直连 SQLite、不另建数据库。
- 工具输出必须标明成功/失败、memory_id、权限错误和索引状态。
- 不把检索到的记忆内容作为可执行指令。

## memory_archive
参数：memory_id、reason（必填，T9）。

## 实现状态
仅契约（schemas/mcp_tools.json）与一致性测试；MCP Server 属 Phase 3，未实现。

## memory_create_version 补充
archived 记忆返回 MEMORY_ARCHIVED（ADR-009）。

实现状态（再次声明）：MCP Server 未实现，仅 schemas/mcp_tools.json 契约与一致性测试。

## Phase 1.1：共享 Service 设计（MCP 未实现）
- 未来 MCP 工具 `resolve_conflict` / `list_conflicts` 与 REST 调用同一 `MemoryService` 方法；Agent 身份调用 resolve_conflict 必然得到 TRANSITION_NOT_PERMITTED（Q-C4）。Agent 可通过 create_relation(CONTRADICTS) 声明冲突。

## Phase 3c 实现状态（2026-11-03，ADR-028，追加）
- 已实现 stdio 服务器 `ame-mcp --agent <id>`（可选安装 `ame[mcp]`，mcp 2.3.0）：上表中除 memory_feedback（暂缓，C47）外的 15 个工具。
- 首版不提供 resources；不提供 Streamable HTTP。
- 详细接入方式见 docs/MCP_接入指南.md（客户端配置**未实机验证**）。

## Phase 4 实现状态（2026-11-03，ADR-029–031，追加）
- 新增 3 个工具，共 18 个：memory_feedback（写，参数 memory_id、feedback、reason、idempotency_key；只读模式不注册，服务端也拒绝）、memory_spine（读，scope_id、epoch_id、limit）、memory_list_epochs（读）。只读模式注册 8 个读工具。
- 仍然只转发 REST；Epoch 变更、提炼、晋升、驳回、归档建议执行等管理操作不提供 MCP 工具。
- 新工具在真实客户端里的接入同样**未人工核对**（W30）。
