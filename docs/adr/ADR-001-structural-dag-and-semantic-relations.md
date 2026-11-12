# ADR-001 图结构分为 Structural DAG 与 Semantic Relations
状态：已接受（2026-11-02，用户决定）

## 决定
- Structural DAG（`memory_edges`：PARENT/REFERENCE/SUPPORTS/SUPERSEDES/DERIVED_FROM）必须执行环检测（跨全部结构边类型），自环拒绝。
- Semantic Relations（新增 `memory_relations`）不执行 DAG 环检测。
- CONTRADICTS 只属于 Semantic Relations，规范化无向存储（low_memory_id < high_memory_id，唯一约束），重复/反向创建幂等，查询双向展示。不得放入严格 DAG。

## 理由
矛盾天然是对称关系，放进 DAG 会导致 A↔B 互斥被误判为环，或为避环而丢失信息。结构关系（派生、引用、取代）需要无环以支持拓扑排序和溯源。

## 影响
- 新表 memory_relations；GraphService 同时负责两类写入。
- 两条 active 记忆建立 CONTRADICTS 时触发状态机 T5。
- 邻居/图查询需同时支持 edge_types 与 relation_types。

## 替代方案
- 全部关系纳入环检测（原规格）：拒绝合法的对称矛盾。
- CONTRADICTS 存两条有向边：冗余、易不一致。
- 允许全图有环：破坏结构溯源与拓扑排序。

---
## 附注（2026-11-02）：被 ADR-007 部分修订
SUPPORTS 不再是 Structural DAG 边，改为有向语义关系；语义关系类型扩展见 ADR-008；边撤销语义见 ADR-006。本 ADR 其余内容继续有效。
