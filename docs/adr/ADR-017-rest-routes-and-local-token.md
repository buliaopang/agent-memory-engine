# ADR-017：Phase 1.2 REST 路由与本地简易门锁

- 状态：已采纳（2026-11-03，用户批准 D1-B / D4-B / D5）
- 关联：API_SPEC、SECURITY #2/#3、Q15（Phase 3）、D-Q4

## 决定
1. REST 适配层（src/ame/adapters/rest/routes.py）只做“解析 → 调 MemoryService → 序列化”，不含业务规则、不含 SQL（测试守护）。MCP（Phase 3）将调用同一 REST 服务（D-Q4）。
2. 已实现接口：POST/GET/PATCH /memories；POST /memories/{id}/status、/classification、/archive、/restore；POST/GET /memories/{id}/versions；POST /edges、/edges/{id}/revoke、GET /memories/{id}/edges；POST /relations、/relations/{id}/revoke、GET /memories/{id}/relations；GET /memories/{id}/audit、/conflicts；POST /conflicts/{id}/resolve。
3. 未实现（明确不做）：/memories/search、/neighbors、/graph（依赖 Phase 2 检索）；/agents、/spaces（Phase 3，随认证）。
4. 门锁：首次启动生成 `secrets.token_urlsafe(32)` 写入 `<data_dir>/api_token`（POSIX 0600；Windows 继承 %LOCALAPPDATA% 的用户 ACL，未加固）。除 /health 外所有接口要求 `Authorization: Bearer <token>`，常量时间比较；令牌不写日志、不回显。错误 401 UNAUTHORIZED。
5. 所有通过认证的调用者在 Phase 3 前一律视为本地用户（actor_type=user, actor_id=user:local），客户端不能自报身份（SECURITY #3）。因此 T1（Agent 写入为 candidate）暂不能经 REST 触发。
6. Idempotency-Key：任何请求带此头一律返回 501 IDEMPOTENCY_NOT_SUPPORTED，不执行写入（Phase 3 实现）。
7. 错误信封统一：AMEError 带 details；请求校验失败 422 VALIDATION_ERROR，只返回字段位置与原因，不回显提交的值；404/405 也用信封。
8. PATCH 只允许 importance / tags（整体替换），必须带 reason，审计为 MEMORY_UPDATED（before/after 不含正文）；archived 记忆不可 PATCH（409 MEMORY_ARCHIVED）。
9. 创建边/关系：新建 201，幂等命中已有 200（created=false）。
10. restore 自动恢复到审计中记录的归档前状态（T10）。

## 不做 / 后续
- DPAPI、文件 ACL 加固、令牌轮换：Q15，Phase 3。
- Agent 凭据与按身份授权、Space ACL：Phase 3。
