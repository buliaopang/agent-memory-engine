# ADR-033：Conflux Tree-Graph 共识机制改造（单父树、最重子树主链、自动 Epoch 与全序、冲突先到者建议）

- 状态：已决议并实现（Phase 5，2026-11-05）
- 关联文档：arXiv:1805.03870v4 (*Scaling Nakamoto Consensus to Thousands of Transactions per Second*)、ADR-001（双层图）、ADR-012（冲突治理）、ADR-030（Epoch）、ADR-031（Spine 评分）

## 背景

在 Phase 1–4 中，AME 的结构层（Structural DAG）已具备 `PARENT`（因果演进边）与 `REFERENCE` / `SUPERSEDES` / `DERIVED_FROM`（引用与演化边）的二元结构，以及基于 5 因子（confirmed / sources / feedback / importance / freshness）的 Spine 节点评分、Epoch 阶段划分与 Kahn 拓扑排序。但与 Conflux Tree-Graph 论文的核心机制相比，此前存在 4 处差距：

1. 未限制每条记忆最多 1 条 active 入向 `PARENT` 边，`PARENT` 边只是一般 DAG 而非严格的父树（Parental Tree）。
2. `SpineService` 仅按单节点 5 因子分数做扁平排序，未沿 `PARENT` 父树累加子树权重并做贪心最重子树（GHOST / Heaviest-Subtree）主链（Pivot Chain）选择。
3. Epoch 完全依赖人工创建与手工加减成员，未由 Pivot Chain 主链通过 DAG 历史可达集 `Past(p_i)` 自动切分 Epoch 并导出确定性全序（Total Order）。
4. 记忆发生 `CONTRADICTS` 冲突时，未利用 Conflux 全序给出“先到者优先（First-Arrived Wins）”的消解建议。

## 决策

### 1. 单 `PARENT` 约束（形成父树 / 父森林）

- 每条记忆最多允许 **1 条 `status = 'active'` 的入向 `PARENT` 边**（方向为 `parent -> child`，即 `to_memory_id` 唯一）。
- **三层防御**：
  1. DTO 层：`MemoryCreate.parent_ids` 去重后若超过 1 个，立即返回 422（多个前驱请通过 `reference_ids` 建立 `REFERENCE` 边）。
  2. 服务层：`GraphService.add_edge` 在幂等检查后，若 `edge_type == PARENT` 且目标记忆已有另一条 active 入向 `PARENT` 边，抛出 `ParentAlreadyExistsError`（`PARENT_ALREADY_EXISTS`，HTTP 409）。重挂父节点需先 `revoke_edge` 撤销旧 `PARENT` 边。
  3. 数据库层：新增迁移 `0016_single_parent`（冻结 `0001`–`0015` 不修改），创建部分索引 `ix_memory_edges_active_parent_child` 与触发器 `trg_memory_edges_single_parent`（`BEFORE INSERT ON memory_edges`），阻止任何绕过应用层的双父写入；`ame check-graph` 在 G008 中实测该触发器，并新增检查项 `G017`（检测历史库中是否存在多条 active 入向 `PARENT` 边）。

### 2. 最重子树主链选择（Heaviest-Subtree Pivot Chain）

- 在 `src/ame/domain/conflux.py` 中实现纯函数、零 I/O 的 Conflux 树图共识计算：
  - 每个节点的自身权重为现有 5 因子 Spine 得分 $w(u) > 0$。
  - 沿 `PARENT` 父树自底向上计算每个节点的子树权重：
    $$\text{SubtreeWeight}(u) = w(u) + \sum_{v \in \text{children}(u)} \text{SubtreeWeight}(v)$$
  - 从子树权重最大的根节点出发（平局按 `memory_id` 字典序最小破局），每一步贪心选择 `SubtreeWeight` 最大的子节点，直至叶子节点，得到真正的 **Pivot Chain（主链）**。

### 3. 由主链自动推导 Epoch 与 DAG 全序（Total Order）

- 对主链上的第 $i$ 个主链记忆 $p_i$，定义 $\text{Past}(p_i)$ 为沿所有 active 结构边（`PARENT` / `REFERENCE` / `SUPERSEDES` / `DERIVED_FROM`，边 `u -> v` 表示 `u` 先于 `v`）可达 $p_i$ 的全部记忆集合（含 $p_i$ 自身）。
- 自动推导的第 $i$ 个 Epoch 为：
  $$\text{Epoch}(p_i) = \text{Past}(p_i) \setminus \bigcup_{j < i} \text{Past}(p_j)$$
- 尚未被主链末端引用的旁支节点归入末尾的 `unanchored` Epoch（`pivot_memory_id = None`），保证图中每条记忆都有全序位置。
- 每个自动推导的 Epoch 内部按 active 结构边做 Kahn 拓扑排序（平局按 `memory_id` 字典序破局），跨 Epoch 按主链顺序拼接，得到唯一确定性的 **Conflux 全序（Total Order）**。
- 原有手工 Epoch（ADR-030）完全保留，自动推导的 Epoch 通过 `GET /api/v1/spine`、`GET /api/v1/epochs/derived`、`ame spine` 与 `ame epoch derive` 暴露。

### 4. 冲突时按全序给出“先到者优先”建议（仍由人确认）

- 当两条记忆 `(memory_a_id, memory_b_id)` 存在 `CONTRADICTS` 冲突时，`ConflictService` / `MemoryService` 计算所在可读空间的 Conflux 全序，并在 `ConflictRead.suggestion`（`ConflictSuggestion`）中返回：
  - `winner_memory_id` / `loser_memory_id`（全序排名更靠前的记忆为 `winner`）
  - `recommended_resolution`（`b_superseded_by_a` 或 `a_superseded_by_b`）
  - `winner_rank` / `loser_rank`、`winner_epoch` / `loser_epoch`、`winner_pivot_memory_id` / `loser_pivot_memory_id` 及可读解释说明。
- **不自动覆盖或自动消解**：遵循 ADR-012（Q-C4），冲突消解仍只能由人类用户调用 `POST /api/v1/conflicts/{conflict_id}/resolve` 确认执行。
