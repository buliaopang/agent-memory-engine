# ADR-008：语义关系类型限定（Q13）

- 状态：已接受（2026-11-02，待人工审核）

## 决策
首版语义关系仅 CONTRADICTS（无向）、SUPPORTS（有向）、RELATED_TO（无向）。不引入 SIMILAR_TO，留待向量阶段，且向量相似度结果不得写入人工声明的 memory_relations。语义关系可撤销（规则同 ADR-006）。只有 CONTRADICTS 触发 T5 与 review_notice。

## 原因
人工声明与机器推断语义不同，混存会污染可信度；类型越少越易一致。

## 影响
RelationType 枚举与 DB CHECK 更新；新增 memory_revoke_relation 契约；RelationRead 增加 directed/status/revoked_*。

## 备选方案
(a) 现在加入 SIMILAR_TO——无向量来源且混淆语义，否决；(b) 开放自定义类型——难以校验，推迟。
