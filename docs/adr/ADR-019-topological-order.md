# ADR-019：结构图拓扑排序

- 状态：已采纳（2026-11-03，用户批准 D3-A）

## 决定
1. 纯函数 `ame.domain.topology.topological_order`：Kahn 算法，同层按 memory_id 排序，结果稳定；边 (from → to) 表示 from 排在 to 之前；孤立记忆也包含；发现环抛 DAG_CYCLE_DETECTED（理论上数据库已阻止）。
2. 只使用 status=active 的结构边（PARENT/REFERENCE/SUPERSEDES/DERIVED_FROM，可按类型筛选）；语义关系与已撤销边不参与。
3. TopologyService 用只读连接（mode=ro + query_only）读取，永不改库；输出只有 memory_id，不含正文。
4. 命令 `ame graph-order [--db] [--json] [--limit N] [--edge-type T]`，默认最多返回 1000 个、上限 100000，超出标记 truncated。
5. 不新增 REST 接口（API_SPEC 未定义）；供 Phase 2 上下文组装内部使用。
