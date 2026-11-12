# ADR-027：Idempotency-Key（防重复提交）与并发写入

- 状态：已采纳（2026-11-03，用户批准 PHASE_3_REVIEW.md 全部推荐，记录保存 24 小时；Phase 3b）
- 关联：ADR-010（WAL + BEGIN IMMEDIATE）、ADR-017（原 501 拒绝）、ADR-024（身份）

## 决定
1. 迁移 0011 新建 `idempotency_keys(actor_id, idem_key, request_hash, state, status_code,
   response_body, created_at, expires_at)`，主键 (actor_id, idem_key)。它只是“回放缓存”，不是事实来源，不进审计。
2. 写方法（POST/PUT/PATCH/DELETE）带 `Idempotency-Key`（1–255 个可见 ASCII 字符，否则 422 VALIDATION_ERROR）：
   - 键按调用者隔离：actor_id 来自认证后的身份（用户 / 各 Agent），不同调用者用同一键互不影响。
   - 请求指纹 = sha256(方法, 路径, 查询串, 原始请求体)。
   - 第一次：在一个 BEGIN IMMEDIATE 事务里插入 pending 预约（主键保证并发时只有一个能抢到），执行请求，
     结果 < 500 则保存状态码与响应体（done）；≥ 500 或异常则删除预约，允许重试再执行。
   - 再次同键同指纹：done → 原样返回保存的状态码与响应体，加响应头 `Idempotency-Replayed: true`；
     pending → 409 IDEMPOTENCY_IN_PROGRESS。
   - 同键不同指纹（不同内容或不同路径）→ 422 IDEMPOTENCY_KEY_MISMATCH（按 PHASE_3_REVIEW 测试计划与 IETF 草案）。
   - 4xx 结果同样保存并回放（与第一次结论一致）。
   - GET/HEAD 带键：忽略（本身可重复），不存记录。认证失败：照常 401，不存记录。
3. 保存 24 小时（`AME_IDEMPOTENCY_TTL_HOURS`，默认 24）。过期后：每次新预约顺带清理最多 100 条、服务启动时全部清理、
   `ame idempotency purge` 手动清理；`ame idempotency status` 查看数量。清理只删幂等记录，绝不碰审计。
4. 并发：保持 WAL + 所有写事务 BEGIN IMMEDIATE + busy_timeout 30s；版本竞争靠 expected_version → 409 VERSION_CONFLICT。

## 取舍
- 预约与业务写入不在同一个事务（业务服务各自开事务）。若进程恰好在“业务已提交、结果未保存”之间崩溃，
  该键会停在 pending，24 小时内重试得到 409 而不会重复写入（宁可拒绝，不重复写）。
