# ADR-034：零依赖静态图导出、可选本地 FastEmbed ONNX 向量与 Wheel 独立打包

- 状态：已决议并实现（Phase 5，2026-11-05）
- 关联文档：ADR-021（派生向量索引）、ADR-022（本地 Embedding 约束）、ADR-025（Space ACL）、ADR-033（Conflux 树图共识）

## 背景

在完成 Conflux 树图共识（ADR-033）与核心安全防线后，为进一步提升本地图谱可视化体验、开箱即用的本地语义向量能力以及独立分发便利性，引入静态交互式 SVG 图导出、可选本地 FastEmbed ONNX 向量推理及 Wheel 内嵌迁移脚本三项能力，同时严格保持 append-only 触发器与 Space ACL 边界。

## 决策

### 1. 静态自包含图导出（`export-graph`，零 CDN，支持 Conflux 主链高亮）

- 新增 `GraphExportService`（`src/ame/application/graph_export_service.py`）：
  - 严格通过 `UnitOfWork` 与 `Access`（`resolve(uow, actor)`）读取数据，遵循 Space ACL（不可见空间返回 404，绝不在路由或 CLI 中写裸 SQL）。
  - 导出节点（含 `on_pivot_chain`、`subtree_weight`、`epoch_index`、`total_order_rank`）、active 结构边（区分普通结构边、`PARENT` 父树边与 Conflux 主链边）、active 语义关系，以及 `pivot_chain`、`derived_epochs`、`total_order`。
  - HTML 模式生成单文件自包含交互式 SVG 页面（零外部 CDN/JS/CSS），支持切换“显示语义关系（虚线）”和“高亮 Conflux 主链（Pivot Chain）”。
- 入口：
  - CLI：`ame export-graph --out <path> [--scope <id>] [--format html|json] [--db <path>]`
  - REST：`GET /api/v1/graph/export?scope_id=...&format=html|json`

### 2. 可选本地 ONNX 语义向量（`FastEmbedProvider` + 自动降级）

- 新增 `src/ame/infrastructure/embeddings.py`：
  - `Settings.embedding_provider` 允许 `"none"`（默认）或 `"fastembed"`（本地 ONNX 推理，默认模型 `BAAI/bge-small-zh-v1.5`），仍然严禁任何云端 Embedding API（如 `"openai"`，保持 SECURITY.md #10 与 ADR-022 约束）。
  - `FastEmbedProvider` 延迟导入可选依赖 `fastembed`（已加入 `pyproject.toml` 的 `[project.optional-dependencies] vector`）。
  - `build_vector_backend(settings)` 在 `embedding_provider="fastembed"` 时组装 `FastEmbedProvider` + `LanceDBVectorStore`（或 `InMemoryVectorStore`）；若未安装 `fastembed`/`lancedb` 或模型加载失败，捕获 `VectorUnavailableError` 并如实降级为 `None`（即 `lexical_graph` 模式），绝不伪造随机或哈希向量分数。

### 3. Wheel 独立打包内嵌迁移脚本

- 在 `pyproject.toml` 的 `[tool.hatch.build.targets.wheel]` 中通过 `force-include = {"migrations" = "ame/_migrations"}` 将 Alembic 迁移脚本打入 Wheel 包。
- `src/ame/infrastructure/database.py` 中的 `MIGRATIONS_DIR` 优先使用仓库根目录下的 `migrations/`，当脱离源码目录通过 Wheel 安装运行时自动回退至包内 `ame/_migrations/`。
