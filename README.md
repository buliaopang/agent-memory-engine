# Agent Memory Engine (AME)

**Agent Memory Engine (AME)** 是一个面向多 Agent 协作的本地记忆引擎，采用 **SQLite 权威存储 + Structural DAG 与 Conflux 树图共识 + 四层记忆模型 + 多空间权限隔离（Space ACL）**，并通过 **REST API** 与 **MCP (Model Context Protocol)** 双入口为 Claude Desktop、Cursor、Codex 等 AI 客户端提供统一的长期记忆服务。

## 核心特性

- **双层图结构（Structural DAG + Semantic Relations）**
  - **结构层（`memory_edges`）**：支持 `PARENT`（单父树约束）、`REFERENCE`、`SUPERSEDES`、`DERIVED_FROM`，内置服务层与 SQLite 触发器双重环路检测（Cycle Guard）。
  - **语义层（`memory_relations`）**：支持 `CONTRADICTS`（冲突自动标记与裁决工作流）、`SUPPORTS`、`RELATED_TO`。
- **Conflux Tree-Graph 主链共识与全序推导**
  - 沿 `PARENT` 父树自底向上计算子树权重，基于最重子树规则（Heaviest-Subtree / GHOST）自动选举 **Pivot Chain（主链）**。
  - 通过主链节点的历史可达集 $\text{Past}(p_i)$ 自动切分 **Epoch** 并导出确定性全序（Total Order），为冲突记忆提供“先到者优先（First-Arrived Wins）”的消解建议。
- **四层正交记忆与六状态生命周期**
  - 层级（`L1`–`L4`）与类型（`working` / `episodic` / `semantic` / `procedural`）正交解耦。
  - 六状态受控流转（`candidate` / `active` / `conflicted` / `superseded` / `archived` / `rejected`），Agent 写入默认为 `candidate`，仅用户本人可审核晋升（`promote`）或裁决冲突。
- **多 Agent 身份认证与空间隔离（Space ACL）**
  - 支持 `shared`、`project`、`private` 三类空间与细粒度读写授权；未授权空间在检索前即被过滤（不可见一律返回 `404`）。
  - Windows 下支持使用系统 **DPAPI** 加密存储本地令牌与 Agent 密钥。
- **混合检索与优雅降级**
  - 内置 SQLite FTS5 trigram 索引 + 中文去空格紧凑索引（CJK Compact Index）+ 图邻居扩展。
  - 可选接入本地 **LanceDB + FastEmbed (ONNX)** 向量检索，通过 Transactional Outbox 异步同步；未安装向量依赖时自动以 `lexical_graph` 模式无损运行。
- **防篡改审计与完整性自检**
  - 记忆版本（`memory_versions`）与统一审计日志（`audit_events`）均由数据库触发器强制 Append-Only。
  - 内置 `ame check-graph`（G001–G017 完整性检查）、`ame backup / restore`（SQLite Backup API 热备）与静态交互式 SVG 图导出（`ame export-graph`）。

## 相关论文与致谢

本项目的 **Conflux Tree-Graph 主链共识机制**（最重子树 Pivot Chain 选举、基于 Past(p_i) 历史可达集的自动 Epoch 划分与确定性全序推导、冲突“先到者优先”消解建议）设计受区块链共识协议 **Conflux Tree-Graph** 启发，核心思想参考自以下论文：

> **Chen et al., "Conflux: A Tree-Graph Protocol, Acting Recursively for Blockchains"**, Proceedings of the 2019 IEEE Symposium on Security and Privacy (S&P), pp. 104–121. arXiv: [1810.12638](https://arxiv.org/abs/1810.12638)

具体映射关系与改造细节见 [docs/adr/ADR-033-conflux-tree-graph-consensus.md](docs/adr/ADR-033-conflux-tree-graph-consensus.md)。AME 为本地记忆引擎，与 Conflux 区块链项目无代码或组织关联。

## 快速开始

### 1. 环境要求与安装

需要 Python 3.12+，推荐使用 [uv](https://docs.astral.sh/uv/) 管理环境：

```powershell
# 安装核心与开发/MCP依赖
uv sync --extra dev --extra mcp

# 如需启用本地向量索引与 ONNX Embedding（可选）
uv sync --extra dev --extra mcp --extra vector
```

### 2. 初始化数据库与启动服务

```powershell
# 执行数据库迁移
uv run ame migrate

# 以后台无窗口守护进程模式启动本地 REST 服务（默认监听 http://127.0.0.1:8765，立即返回不占终端）
uv run ame-server --daemon

# 如需重启或停止后台服务
uv run ame-server --restart
uv run ame-server --stop
```

查看本地用户令牌（用于 REST API `Authorization: Bearer <token>`）：

```powershell
uv run ame token show
```

### 3. 常用 CLI 命令

```powershell
# 图与数据库完整性检查（G001–G017）
uv run ame check-graph

# 备份与恢复数据库
uv run ame backup
uv run ame restore <backup_file_path>

# 注册 Agent 并配置 MCP 密钥
uv run ame agent create agent:codex --agent-type codex
uv run ame mcp setup --agent agent:codex

# 查看 Conflux 主链（Pivot Chain）与自动推导的 Epoch 全序
uv run ame spine
uv run ame epoch derive

# 导出零依赖自包含交互式 SVG 记忆图谱
uv run ame export-graph --out memory_graph.html --format html

# 记忆提炼、审核与衰减建议
uv run ame distill run --scope shared --reason "定期提炼"
uv run ame review list
uv run ame decay suggest
```

## 接入 MCP 客户端

详细步骤请参考 [docs/MCP_接入指南.md](docs/MCP_接入指南.md)。在客户端配置中添加 `ame-mcp`：

```json
{
  "mcpServers": {
    "ame": {
      "command": "ame-mcp",
      "args": ["--agent", "agent:claude"],
      "env": {
        "AME_MCP_ENABLED": "true",
        "AME_MCP_READ_ONLY": "false"
      }
    }
  }
}
```

## 运行测试与代码检查

```powershell
uv run pytest -q
uv run ruff check .
uv run ruff format --check .
uv run mypy --strict src
```

## 文档目录

- [总体架构说明 (ARCHITECTURE.md)](docs/ARCHITECTURE.md)
- [数据模型与持久化规范 (DATA_MODEL.md)](docs/DATA_MODEL.md)
- [记忆状态机规范 (STATE_MACHINE.md)](docs/STATE_MACHINE.md)
- [REST API v1 规范 (API_SPEC.md)](docs/API_SPEC.md)
- [MCP 接口规范 (MCP_SPEC.md)](docs/MCP_SPEC.md)
- [MCP 客户端接入指南 (MCP_接入指南.md)](docs/MCP_接入指南.md)
- [安全与隐私设计 (SECURITY.md)](docs/SECURITY.md)
- [架构决策记录 (docs/adr/)](docs/adr/)
