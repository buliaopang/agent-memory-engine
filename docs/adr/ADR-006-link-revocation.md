# ADR-006：结构边与语义关系的撤销语义（Q11）

- 状态：已接受（2026-11-02，待人工审核）

## 决策
memory_edges / memory_relations 增加 status（active/revoked）及 revoked_at、revoked_by、revoked_actor_type、revoked_reason。撤销只改状态、保留原记录，禁止删除与回改（触发器）。有效 DAG、环检测、parent/reference 解析只看 active；历史查询 include_revoked=true 可返回已撤销边。重建关系必须新建 edge_id。SUPERSEDES 只能经状态机 T11 撤销，T11 自动撤销指向该记忆的 active SUPERSEDES 边。

## 原因
保留完整历史以便追溯；避免仅凭状态和审计“推断”边是否有效；物理删除违反“不删除旧记忆”原则。

## 影响
迁移 0003 重建两张表；三元组唯一约束改为 active 部分唯一索引；Repository 新增 revoke/list_for(include_revoked)；EdgeRead 增加字段；新增 memory_revoke_edge 契约。

## 备选方案
(a) 物理删除边——丢失历史，否决；(b) 仅在审计表记录——查询有效 DAG 需联表推断，易错，否决；(c) 撤销后复用 edge_id 重新激活——破坏不可变性，否决。
