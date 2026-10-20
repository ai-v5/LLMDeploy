# 双后端会话路由

`python -m deploy_gateway` 在本地 SSH 转发端口前提供兼容 OpenAI 的 HTTP/SSE 入口。两台健康时按已绑定会话数分配新会话；已有会话保持同一机器和 DP rank。仅剩一台时使用健康机器，故障迁移后保持新绑定，原机器恢复后参与新会话分配。绑定持久化，网关重启不主动重新分配。

```sh
python -m deploy_gateway --key-store local/gateway-keys.json --bindings-file local/session-bindings.json --backend-state local/backend-state.json
```

默认只监听 loopback，后端为本机 `19001`、`19002`，健康检查每两秒一次；失败立即摘除，连续两次健康检查通过后恢复参与分配。需要远程客户端时显式配置 `--listen` 和网络访问范围。升级网关前等待在途请求结束，保留旧程序及私有状态用于回退；不会操作远端 vLLM。

认证存储使用现有网关的 version 1 格式：`records` 中每项含 API key 的 SHA-256 `sha256` 和布尔值 `active`。所有客户端请求必须提供有效 Bearer key；Authorization 不转发给模型。认证存储、绑定文件、状态文件及其临时文件必须使用互不重叠的私有路径。这些运行文件均不得进入 Git，不提供含认证信息的示例配置。

客户端应传入稳定的会话头，优先级依次为 `x-session-affinity`、`x-deepseek-harness-session-id`、`x-session-id`；长度为 1–256 字符。缺少这些头时使用 `prompt_cache_key`，再缺少时使用第一条 user 消息作为近似会话标识。不同会话首条消息相同、历史压缩改变首条消息时，近似标识无法可靠区分会话，需显式会话头。绑定按认证 key 隔离，仅保存不可逆指纹和本地端口，不保存会话原文或请求内容。默认最多保存 16384 个会话；满额时拒绝新绑定，已有绑定继续工作，需要运维明确处理，不自动淘汰导致回迁。

连接失败或后端 5xx 时，仅在尚未向客户端发送响应头前尝试另一台。开始返回 SSE 后发生故障会结束该流，客户端可重新发起完整请求；无法搬迁正在生成的 KV 状态。网关不记录请求、响应或认证材料。慢请求的上游读取等待默认为 10800 秒，可用 `--upstream-timeout` 调整；它不是模型端的执行时限。

两台模型版本、图片和上下文限制应保持一致；上下文不一致时用 `--model-context-limit` 指定共同支持的下限。它保守收紧 `/v1/models` 的 `max_model_len` 声明，故障切换时也不把单台实验配置当作整个入口的能力。请求 token 校验仍由模型执行，不会静默截断输入。`backend-state.json` 的 `active_ports` 表示可用池，`active_port` 仅保留一个代表端口供旧消费者使用，不代表所有请求只在该端口执行。

需要给特定客户端开放长上下文时，为每个后端配置 `--backend-context-limit PORT=TOKENS`。客户端发送 `x-model-context-window`（总上下文 tokens，包含输出），只会使用支持该容量的健康后端，模型列表也按该请求的容量声明。缺少此头的客户端继续使用普通均衡与共同上限。没有符合容量的健康后端时返回 503，不降级到短上下文机器；会话迁移后保持新绑定。此头不转发给模型，实际请求仍由模型校验且不截断。只有一台支持长上下文时，该容量暂不具备双机故障切换能力。
