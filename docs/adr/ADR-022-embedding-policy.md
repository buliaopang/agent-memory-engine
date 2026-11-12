# ADR-022：Embedding 策略（默认无模型、离线、无云端）

- 状态：已采纳（2026-11-03，用户批准 P2-1 A、P2-2 A、P2-3 A）

## 决定
1. 默认不安装、不下载任何模型。配置项 `embedding_provider` 目前只接受 "none"，此时系统运行在 lexical_graph 模式。
2. 本阶段**没有实现**任何真实的本地模型 Provider，也没有手动下载命令。以后加入时必须做到三点：只能由用户手动执行下载命令，之后完全离线可用，并且另立 ADR。
3. 不使用任何云端 Embedding API，因为记忆正文不能离开本机。
4. 测试使用确定性的 HashingEmbeddingProvider（tests/phase2/fakes.py）。它只存在于 tests 目录，产品配置无法选用。
5. 向量侧出现任何异常都会降级为 lexical_graph，并在 degraded_reason 中只写异常类型，搜索本身照常返回结果。
