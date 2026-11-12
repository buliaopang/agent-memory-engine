# ADR-015：archived / rejected 记忆不得新增链接
- 状态：已接受（2026-11-02，Q-N11）
- 决定：端点为 archived 或 rejected 的记忆不能新增结构边或语义关系（Service 层 LinkEndpointFrozenError 409 + 数据库触发器 LINK_ENDPOINT_FROZEN 兜底）。撤销已有链接仍允许。已有的历史链接不受影响。
- 推论：从 archived 恢复到 superseded（T10）不再创建新 SUPERSEDES 边，`superseded_by_memory_id` 只用于 T7/T8。
