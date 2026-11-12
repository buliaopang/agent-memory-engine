# ADR-004 Layer 与 Memory Type 正交
状态：已接受（2026-11-02）

## 决定
Layer ∈ {L1..L4}，memory_type ∈ {working, episodic, semantic, procedural}，两者独立存储、任意组合合法，不强制一一映射。晋升可改 Layer；修改 memory_type 必须提供理由（否则 REASON_REQUIRED），所有分类变化写审计。

## 理由
Layer 表示存储/检索层级与稳定度，Type 表示认知类别；同一类别可处于不同层级。

## 影响
- 检索参数新增 memory_types，与 layers 并列。
- ClassificationChange 用例与审计事件 classification_changed。

## 替代方案
- L1=working…L4=procedural 固定映射：表达力不足，晋升会被迫改变类型。
