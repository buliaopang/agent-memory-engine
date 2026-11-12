# ADR-014：备份、恢复与安全迁移
- 状态：已接受（2026-11-02，Q-E1/Q-E2）
- 决定：
  1. 备份一律用 SQLite backup API（包含 WAL 中尚未 checkpoint 的数据）；只复制 .db 文件会丢数据，禁止。
  2. 迁移运行器：已有库升级前 integrity_check → 自动备份到 `data_dir/backups/ame-pre-migrate-<rev>-<UTC时间>.db` 并校验 → alembic upgrade → 后检查（integrity、revision、所需触发器、外键违规不增加、行数不减少；0003 的 SUPPORTS 迁移按边+关系合计）→ 失败自动恢复备份，失败现场另存为 `pre-restore-*.db`，抛 MigrationFailedError。
  3. 备份永不覆盖、永不自动删除（Q-E1）。
  4. 0003、0005 不提供结构降级；回退 = 恢复备份（Q-E2）。0004 downgrade 仅删除触发器与索引。
  5. CLI：`ame backup [--db] [--dest]`、`ame restore <备份> [--db] --yes`（恢复前把当前库另存）、`ame migrate [--db]`。恢复前须停止 AME 服务。
- 风险：SQLite 上 Alembic DDL 非事务性，中途失败依赖备份恢复。
