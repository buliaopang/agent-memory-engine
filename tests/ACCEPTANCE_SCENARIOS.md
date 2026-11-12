# 端到端验收场景

## A. 跨 Agent 共享
1. 注册 codex、dsh。
2. 建立 project space，并将两者设为 editor。
3. Codex 写入记忆。
4. DSH 搜索并读取。
5. 断言内容、来源、memory_id 一致。

## B. 私有空间隔离
1. DSH 在 private:dsh 写入。
2. Codex 查询相同关键词。
3. 断言结果不包含该记忆。
4. Codex 直接 GET 该 memory_id，断言 404（D-Q1：不泄漏存在性）。

## C. DAG 环检测
1. 创建 A→B、B→C。
2. 尝试 C→A。
3. 断言拒绝，数据库不出现该边。

## D. 版本冲突
1. 读取 memory，current_version=1。
2. 两个 Agent 都以 expected_version=1 创建新版本。
3. 第一个成功，第二个返回 409。
4. 原版本仍可读。

## E. 向量索引恢复
1. 创建记忆并等待 outbox。
2. 删除测试向量索引。
3. 执行 rebuild。
4. 搜索可重新召回记忆。

## F. 无 Embedding 降级
1. 禁用 embedding provider。
2. 写入和关键词检索仍可用。
3. 响应 retrieval_mode=lexical_graph。
4. 不得伪造相似度分数。
