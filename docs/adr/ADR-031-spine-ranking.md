# ADR-031：主链排序（spine）

- 状态：已采纳（2026-11-03，用户批准 PHASE_4_REVIEW.md 的 D1–D12；Phase 4）
- 关联：D6

## 决定
1. `GET /api/v1/spine?scope_id=&epoch_id=&limit=`、MCP `memory_spine`：只排 active 记忆，只读，不建表、不写库。
2. 五个因素（0–1）：confirmed（用户自己写的 1.0；Agent/系统写、经用户晋升的 0.7）、sources（来源数 / 3，封顶 1）、feedback（(反馈分 + 1) / 2）、importance、freshness（0.5^(天数 / AME_SPINE_FRESHNESS_HALF_LIFE_DAYS，默认 90)）。
3. 默认权重 confirmed 0.25、sources 0.15、feedback 0.20、importance 0.20、freshness 0.20；可用 `AME_SPINE_WEIGHTS="confirmed=0.3,sources=0.1,..."` 修改，没写的保持默认，合计必须等于 1，否则启动失败。
4. 每条结果都带 components、weights 和 5 行中文可读的加分说明；排序相同时按 memory_id，结果稳定。
5. 主链只是一种排序，不是“真相”，不影响写入和状态。

## 取舍
- 每次全量计算（2000 条 p95 约 34 ms，Linux）。
