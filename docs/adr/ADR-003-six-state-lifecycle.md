# ADR-003 六状态状态机
状态：已接受（2026-11-02）

## 决定
实现 candidate/active/conflicted/superseded/archived/rejected 状态机，规则见 docs/STATE_MACHINE.md（T1–T12）。每次转换记录操作者类型、操作者、规则编号、原因到 append-only 审计表。历史版本不可覆盖（触发器保障）。S1–S6 按推荐：用户手录直接 active；candidate×active 矛盾只记关系并提醒；superseded 可由用户撤销；rejected 可重新提交；只有用户本人审核；搜索默认隐藏 conflicted。

## 理由
记忆可信度需要可解释的生命周期，避免 Agent 自我晋升和无痕修改。

## 影响
- 新表 memory_audit_events；状态只能经 MemoryService.change_status / 关系创建改变，不经 PATCH。
- 恢复归档需从审计读取归档前状态。

## 替代方案
- 自由状态字段：无法审计、无法防止越权。
- 版本化整个记录（event sourcing）：MVP 复杂度过高。
