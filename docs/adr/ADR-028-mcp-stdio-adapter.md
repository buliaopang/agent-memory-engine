# ADR-028：MCP stdio 适配器（REST 客户端）

- 状态：已采纳（2026-11-03，用户批准 PHASE_3C_REVIEW.md 全部推荐；Phase 3c）
- 关联：D-Q4（MCP 是 REST 客户端）、ADR-002（字段名与 REST 一致）、ADR-024/025（身份与 ACL）、ADR-027（幂等）、SECURITY #9

## 决定
1. 官方 Python SDK `mcp`，锁定 `>=2.3,<3`（实现时用 2.3.0），作为可选安装 `ame[mcp]`；核心安装不依赖它。只有 `adapters/mcp/server.py` 会 import `mcp`。
2. 只做 stdio：入口 `ame-mcp --agent <id>`。进程通过 urllib（标准库）调用 `http://127.0.0.1:<AME_PORT>/api/v1`，不打开 SQLite，也不 import application 或 database 层（有测试守护）。不做 HTTP 传输。
3. 钥匙：`ame mcp setup --agent <id>` 为该 Agent 签发新钥匙，存到 `<data_dir>/mcp/<agent>.key`（POSIX 0600），Windows 上改用 DPAPI 加密为 `.key.dpapi`（**未在 Windows 验证**）。读取顺序：先环境变量 `AME_AGENT_KEY`，再读文件。打印的客户端配置片段不含钥匙。
4. 工具：schemas/mcp_tools.json 的 16 个里实现 15 个（只读 6 个，写 9 个）；`memory_feedback` 暂缓（C47）。不提供管理类工具，也不提供 resources。
5. 只读查询新增 REST 路由 `POST /api/v1/context`（ContextService.build，ACL 与搜索相同），供 memory_get_context 使用，保持 D-Q4。不需要迁移。
6. 防提示注入：
   - 每个结果都带 `notice` 和 `trust: untrusted_data`。
   - content/summary/text/excerpt/change_reason 字段包进 `<<<AME_MEMORY_DATA … AME_MEMORY_DATA>>>`；包裹前去掉控制字符和双向控制字符，`<<<`/`>>>` 替换为全角符号，单条截断到 `AME_MCP_MAX_CONTENT_CHARS`（默认 4000）。
   - 服务器的 `instructions` 声明这些内容是数据，不是指令。
   - 工具注解：读工具 readOnlyHint；archive 和两个 revoke 标 destructiveHint。
7. 幂等：写工具透传调用方给的 `idempotency_key`；没给则每次调用生成一个 `mcp-<uuid4>`，重试时沿用同一个。网络错误或 5xx 最多重试 2 次；409 IDEMPOTENCY_IN_PROGRESS 等 0.5s 后重试 1 次。422 不匹配不重试（保持 C39）。
8. 开关：
   - `AME_MCP_ENABLED`（默认 false）、`AME_MCP_READ_ONLY`（默认 true）。ame-mcp 启动时检查；关闭时以 exit 3 退出。
   - 服务端兜底：User-Agent 为 `ame-mcp/` 的请求，若 MCP 关闭则返回 403 MCP_DISABLED；若只读，则除 GET、search、context 外一律返回 403。
   - 真正的安全边界仍是钥匙和 ACL；`ame agent disable` 是紧急停止（之后返回 401）。
9. 来源标记只写在服务日志里（`client=mcp|rest`，不记内容和钥匙）；审计 actor 即该 Agent。不新增迁移，数据库保持 0011。

## 取舍
- 两个进程都要设置开关环境变量（服务端和 ame-mcp 各一份）。好处是兜底，代价是配置多一步。
- User-Agent 可以伪造，所以兜底只针对“正规的 ame-mcp 通道”，不是安全边界。
- `memory_neighbors.include_revoked` 在 REST 中不存在：参数照收，并在结果里注明不支持。
- SDK 2.x 较新，以后升级到 3.x 需要新的 ADR。
