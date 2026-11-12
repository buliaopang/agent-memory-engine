# ADR-009：archived 记忆禁止创建新版本（Q14）

- 状态：已接受（2026-11-02，待人工审核）

## 决策
archived 记忆禁止 create_version（Service 抛 MEMORY_ARCHIVED / 409），必须先 restore（T10，记录 actor、时间、原因）。数据库触发器同时阻止为 archived 记忆插入 memory_versions 及修改 memories.current_version，直接写库同样被拒。

## 原因
归档表示冻结；静默修改归档内容会破坏审计与用户预期。

## 影响
新增错误码 MEMORY_ARCHIVED；迁移 0003 新增两个触发器；API/MCP 契约补充错误。

## 备选方案
(a) 允许并自动恢复——隐式状态变化，否决；(b) 仅 Service 层校验——直写可绕过，否决。
