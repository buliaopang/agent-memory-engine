# ADR-002 全项目统一命名
状态：已接受（2026-11-02）

## 决定
数据库列、REST、MCP、JSON Schema、DTO 统一使用：memory_id、current_version、version_id、version（具体版本序号）、edge_id、relation_id。补充：结构边端点 from_memory_id / to_memory_id；来源 source_id；审计 audit_id。禁止裸 `id` 列和同义异名。

## 理由
原规格中 `id`/`memory_id`、`version`/`current_version`、`source_id`（边端点）/来源 ID 混用，会导致适配层映射错误和契约漂移。

## 影响
- 边端点由 source_id/target_id 改为 from_memory_id/to_memory_id（避免与 memory_sources.source_id 冲突），API/MCP 契约随之修订。
- memory.schema.json 等由 tools/gen_schemas.py 从 DTO 生成，测试保证 DTO、DB、MCP 契约一致。
- 取代之前“API 用 memory_id、内部用 id、DTO 映射”的方案（D-Q5 原推荐）。

## 替代方案
- DTO 层映射内外不同名：增加映射代码和出错面，已被否决。
