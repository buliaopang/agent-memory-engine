# ADR-024：Agent 身份认证（每个 Agent 一把钥匙，只由用户发放）

- 状态：已采纳（2026-11-03，用户批准 PHASE_3_REVIEW.md 全部推荐；Phase 3a）
- 关联：ADR-017（本地令牌）、ADR-018（Agent 注册表）、ADR-025、ADR-026

## 决定
1. 请求方身份只由 `Authorization: Bearer <token>` 决定，请求体/请求头都不能自报身份：
   - 用户门锁令牌 → `user:local`
   - Agent 钥匙 `ame_agt_<43 位随机字符>`（256 位）→ actor_type=agent，actor_id=该 agent_id
   - 其他、或 Agent 已禁用 → 401
2. 钥匙只能由用户发放/轮换（CLI `ame agent create|rotate-key`，REST `POST /agents/{id}/credentials`）。
   明文只返回一次；数据库只存 `sha256$<hex>`。轮换后旧钥匙立刻失效。
3. 因钥匙是 256 位随机数，用不加盐 SHA-256 做校验值即可（无需慢哈希），并可按校验值直接查找；比较用常量时间。
4. 钥匙/令牌不写日志、不写审计（审计只记 `rotated: true/false`）。
5. Agent 的写入一律从 candidate（待审核）开始（T1，原状态机规则不变），可通过 REST 实现。

## 后果
- MCP（3c）将复用同一 AuthService；3a 未实现 MCP。
- 用户令牌轮换后需重启服务才生效（ADR-026）。
