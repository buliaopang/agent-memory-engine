# ADR-016：SQLite 版本策略
- 状态：已接受（2026-11-02，Q-F1）
- 决定：使用 Python 自带的 sqlite3，不额外打包 SQLite。要求 ≥ 3.35.0（部分索引、递归 CTE、窗口函数、RETURNING），创建引擎与迁移前检查，低于则报 StorageError。Windows 版 Python 3.13 自带版本满足要求（需 Windows 实机验收确认）。
