# MCP 接入指南

## 1. 安装
```powershell
uv pip install -e ".[mcp]"      # 或 pip install "agent-memory-engine[mcp]"
```

## 2. 开启并以后台无窗口模式启动服务
设置环境变量并以静默守护进程方式启动 `ame-server`（不会占用当前终端，也不会在会话框中留下运行中任务）：
```powershell
setx AME_MCP_ENABLED true
setx AME_MCP_READ_ONLY false
setx AME_AUTO_APPROVE_AGENT_WRITES true
$env:AME_MCP_ENABLED = "true"
$env:AME_MCP_READ_ONLY = "false"
$env:AME_AUTO_APPROVE_AGENT_WRITES = "true"
ame-server --daemon
```
> 说明：`ame-mcp` 在被客户端拉起或调用时，若检测到本机 `127.0.0.1:8765` 未运行，也会自动在后台静默拉起 `ame-server` 守护进程（默认开启 `AME_AUTO_APPROVE_AGENT_WRITES=true`，Agent 写入直接生效为 `active` 状态，无需手动审批），无需在 AI 聊天框中手动执行后台重启命令。如需重启服务，可执行 `ame-server --restart`。

### Windows 开机自启脚本（可选）
在 `%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup\ame-server.cmd` 中写入：
```cmd
@echo off
set AME_MCP_ENABLED=true
set AME_MCP_READ_ONLY=false
set AME_AUTO_APPROVE_AGENT_WRITES=true
"C:\Users\Admin\.ame\app\.venv\Scripts\ame-server.exe" --daemon
```

## 3. 给某个 AI 工具发放钥匙
```powershell
ame agent create agent:codex --agent-type codex --no-key   # 已注册过可跳过
ame mcp setup --agent agent:codex
```
钥匙会加密保存在本机，屏幕上打印的配置片段里**不包含明文钥匙**。注意：这一步会轮换新钥匙，该 Agent 的旧钥匙随即失效。

## 4. 客户端配置示例（mcp.json / config.toml）
- **Codex**：`~/.codex/config.toml`
```toml
[mcp_servers.ame]
command = "ame-mcp"
args = ["--agent", "agent:codex"]
env = { AME_MCP_ENABLED = "true", AME_MCP_READ_ONLY = "false", AME_AUTO_APPROVE_AGENT_WRITES = "true" }
```
- **Claude Desktop**：`%APPDATA%\Claude\claude_desktop_config.json`
- **Cursor / WorkBuddy**：`~/.cursor/mcp.json` 或 `~/.workbuddy/mcp.json`

JSON 格式如下（确保 `AME_MCP_READ_ONLY` 为 `"false"`、`AME_AUTO_APPROVE_AGENT_WRITES` 为 `"true"` 以允许自动免审写入记忆）：
```json
{
  "mcpServers": {
    "ame": {
      "command": "ame-mcp",
      "args": ["--agent", "agent:claude"],
      "env": {
        "AME_MCP_ENABLED": "true",
        "AME_MCP_READ_ONLY": "false",
        "AME_AUTO_APPROVE_AGENT_WRITES": "true"
      }
    }
  }
}
```
如果客户端找不到 `ame-mcp`，请将 `command` 改为虚拟环境中的完整路径，例如 `C:\...\.venv\Scripts\ame-mcp.exe`。

## 5. 关闭 / 紧急停止
- 停用单个 Agent：`ame agent disable agent:codex --reason "停止"`，立即生效（之后返回 401）。
- 停止后台服务：`ame-server --stop`。
- 关掉整个 MCP：把 `AME_MCP_ENABLED` 设为 `false` 并执行 `ame-server --restart`，或直接从客户端配置里移除 `ame`。
