# ADR-010：数据库层 DAG 守护与 IMMEDIATE 写事务
- 状态：已接受（2026-11-02，Asia/Shanghai，用户“按推荐来继续”，Q-A1/Q-A2/Q-A3）
- 背景：Phase 1.1 审查发现直接写库可绕过 Service 层环检测；DEFERRED 事务下并发“先查后写”可形成环。
- 决定：
  1. 迁移 0004 增加触发器 `trg_memory_edges_no_cycle_insert/update`（递归 CTE，只看 active 结构边，UPDATE 排除当前行）与端点存在触发器（即使 `foreign_keys=OFF` 也生效）。
  2. 0004 先用 Kahn 算法检查既有数据，发现环即中止迁移（Q-A1），由迁移运行器自动恢复备份。
  3. 写用例一律 `BEGIN IMMEDIATE`，读用例 `BEGIN`（DEFERRED）（Q-A2）；实现为 pysqlite `isolation_level=None` + SQLAlchemy begin 事件 + ContextVar。
  4. Service 层环检测保留（给出友好错误）；触发器是兜底。触发器错误统一在 UnitOfWork 映射为领域错误（409）。
  5. SQLite 最低 3.35，低于则启动报错（Q-F1）。
- 后果：写入串行化（本地单机可接受，实验最坏 5.7 ms/次检查，10 万边链）。触发器可被有权限者删除，由 ADR-011 的 G007/G008 检测。
