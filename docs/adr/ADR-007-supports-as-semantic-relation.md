# ADR-007：SUPPORTS 移出 Structural DAG（Q12）

- 状态：已接受（2026-11-02，待人工审核）

## 决策
SUPPORTS 改为 memory_relations 中的有向语义关系（from=证据 → to=被支持）。结构边（PARENT/REFERENCE/SUPERSEDES/DERIVED_FROM）与语义关系使用独立表、独立 Repository（EdgeRepository / RelationRepository）、独立 Service（GraphService / RelationService）与校验逻辑。ADR-001 部分修订。

## 原因
“支持”是论证关系，可能互相支持，不应受全局无环约束；也不参与结构层级解析。

## 影响
迁移 0003 将现有 SUPPORTS 边迁入 memory_relations（relation_id='rel_'||edge_id），memory_edges 的类型 CHECK 去掉 SUPPORTS；memory_relations 列 low/high 改名 from/to，CHECK 允许 SUPPORTS 不规范化。memory_link 拒绝 SUPPORTS。

## 备选方案
(a) 保留在 DAG——误报环，否决；(b) SUPPORTS 无向——丢失证据方向，否决。
