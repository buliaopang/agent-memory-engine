# ADR-021：Outbox、后台索引线程、向量索引只是派生副本

- 状态：已采纳（2026-11-03，用户批准 P2-4 A、P2-9 A）

## 决定
1. 0007 新增 `outbox_events` 表，由数据库触发器写入：新版本写 `memory.index`，状态变化写 `memory.status`，和写入在同一事务里。payload 只放 memory_id 和 version。这张表禁止删除（有触发器保护，G007/G008 会检查）。迁移时给已有的每条记忆回填一个 memory.index 事件。
2. 新增 `vector_index_state` 表，按 memory_id 记录状态（indexed、failed、removed、disabled）以及版本、模型、维度、内容哈希。它不存正文，也不存向量。
3. Worker 认领事件时把 next_attempt_at 当作租约（60 秒），按 rowid 排序。进程崩溃后，租约过期的事件会被重新认领。
   - 失败后按 2^次数 秒退避，5 次后标为 failed。last_error 只记录异常类型名。
   - `ame index retry-failed` 可以重跑失败事件。
4. 处理一条事件时，从 SQLite 读取当前状态：
   - 没有 provider：标为 disabled（不假装已索引）。
   - archived 或 rejected：从向量库删除，标为 removed。
   - 其余情况：嵌入当前版本正文，然后 upsert。
   - 搜索时只采用版本和模型都与当前一致的向量命中；过期向量不参与打分。
5. 运行方式：服务进程内的后台线程（index_worker_enabled，默认开启，间隔 2 秒），另有命令 `ame index status|run|rebuild|rebuild-lexical|retry-failed`。rebuild 会清空向量库，并为所有记忆重新排入 memory.reindex。
6. 向量库：
   - InMemoryVectorStore：无依赖。
   - LanceDBVectorStore：可选 extra `ame[vector]`，懒加载导入。表里只有 id、版本、模型、维度、哈希、时间和向量，没有正文。
   - 向量库目录删掉也不会丢数据，用 rebuild 重建即可。
7. health 返回 worker 状态（running、stopped、disabled）、待处理和失败的事件数、retrieval_mode。MemoryRead 增加 vector_status 字段（pending、indexed、stale、failed、removed、disabled）。
