# ADR-011：`ame check-graph` 只读完整性检查
- 状态：已接受（2026-11-02）
- 决定：新增 CLI `ame check-graph [--db] [--verbose] [--json] [--repair-dry-run]`，以 `mode=ro` + `query_only` 打开数据库，默认绝不修改数据；`--repair-dry-run` 只输出修复建议。
- 检查项：G001 环；G002 自环；G003 端点缺失；G004 重复 active 边；G005 无向关系未规范化；G006 superseded 无 SUPERSEDES（警告）；G007 守护触发器缺失/被改（与 `ame_meta.trigger_fingerprints` 指纹比对）；G008 在内存克隆库上实测触发器是否生效；G009 integrity_check / foreign_key_check；G010 已撤销链接缺撤销元数据；G011 冲突簿记（conflicted 无 open 冲突=警告；open 冲突指向已撤销关系=错误）。
- 退出码：0 正常，1 仅警告，2 有错误，3 无法检查（文件不存在/打不开/版本低于 0005），64 参数错误。
- 分层：SQL 只在 `infrastructure/integrity_repository.py`；逻辑在 `application/integrity_service.py`；`ame/cli.py` 只做参数与输出。
