# 换机核对单

## 目标机器重新确认

- 硬件：Linux aarch64，单 OS 能看到 8 张 950DT；检查设备健康、运行进程和资源归属。本配方没有宣称适用于其它卡型或拆成多个 OS。
- 软件：参考驱动 `26.2.0.b021`、CANN `9.2.0`；核验目标机真实版本及驱动/设备节点。不同版本需要重新验证，不能复制另一台机器的软件事实作为证据。
- 文件：固定 revision 的全部 83 个权重/配套文件，完整内容校验；正确的运行镜像；本仓两个源码补丁。
- 容量：原始权重约 510GB，另留下载临时文件、镜像归档、Docker 层、编译目录和 runtime 缓存空间；KV 缓存的设备内存占用独立于磁盘。不要将模型放在小容量根分区。
- 主存：Engram 采用 CPU offload 和 DP 共享；这不是 CPU KV offload。新机内存容量和共享内存配置必须满足模型加载；容器 `/dev/shm` 为 256GiB。
- 路径：修改 `local/host.json`，让 model/runtime/artifacts 目录互不嵌套。CANN 路径同时影响环境变量与只读挂载。
- 组网：本机驱动 topo、RDMA/UBURMA、HCCL 可选配置只使用目标机自身文件；不要从其它机器照搬。

## 连接客户端

默认服务只监听目标机 loopback。操作者可在自己的电脑建立 SSH 转发：

```sh
ssh -N -L 19001:127.0.0.1:8900 USER@HOST
```

模型 ID：`ds-v4.1-flash-1017`；直连隧道的 API base：`http://127.0.0.1:19001/v1`。已有独立网关时继续使用原认证管理方式，双机入口见[会话路由](gateway.md)；认证配置留在本机。

dsh 中只需保留这些模型能力字段；提供方类型为 `openai-completions`，认证在本机既有配置中单独管理，不要把完整 profile 复制到仓库：

```json
{
  "id": "ds-v4.1-flash-1017",
  "contextWindow": 1048576,
  "maxTokens": 32768,
  "input": ["text", "image"]
}
```

总窗口为 1,048,576 tokens，含输入和输出；输出请求 32,768 时，输入预算最多为 1,015,808。`maxTokens` 是客户端请求上限，不是本仓声称每个场景都完成了 32K 输出验证。主备窗口一致并各自验证通过后，才能把网关公共窗口及各后端容量一起更新为 1M。

### DeepInfer 的 DSH 上下文预算补丁

[`client-patches/deepinfer-budget`](../client-patches/deepinfer-budget/) 针对 DSH `0.2.1-rc.2` 的固定源码 SHA，在客户端 OpenAI payload 完成序列化后检查预算。窗口仍为 1,048,576，默认输出仍为 32,768；额外预留 16,384，因此完整输入达到 999,424 时进入压缩。输出只扣一次，输入计入系统提示、工具定义、消息模板和特殊 token。原有字符估算仍用于较早的 step 压力检查，不能替代最终请求检查。

计数器使用锁定的 `deepseek-recipe 0.2.0` 和匹配的 V4.1 tokenizer，复用服务端的 developer/system、输出字段归一化及原生模板编码；文字请求使用实际 token 数。图片按同版本服务端预处理器每图最多 1,024 token 预留，属于保守上界，不宣称是图片实际计数。计数失败明确终止发送，不退回字符估算，不截断消息，也不降低输出上限。

用 `cargo build --release --locked --manifest-path client-patches/deepinfer-budget/counter/Cargo.toml` 构建计数器；二进制、tokenizer、生成模块、manifest、真实请求收据和本机配置只放在忽略的 `local/`。在支持读取打包 runtime 的 Electron Node 环境调用 `prepare.cjs RUNTIME_NODE_MODULES OUTPUT_DIR COUNTER_EXE TOKENIZER_JSON`，生成目录必须是新的。生成器核验原始 SHA，生成模块在加载时再次核验；保留上游 MIT 许可，禁止直接改写签名的 `app.asar`。

激活时保留其它 provider，只将 `deepinfer` 转到生成的 adapter；禁用原 `compaction-basic`，插入生成的 compaction 模块，并为该 provider/model 配置 `headroomTokens: 16384`、`maxTokens: 32768`、`maxOverflowRetries: 1`。DSH patch 的 `name` 是身份断言，不能直接用它替换已有模块，必须使用 disable/insert。配置备份保留在原 profile 旁，先核对并发改动再写入，禁止复制或覆盖 session 文件。

明确的上下文 400 和发送前预算不足均使用既有的 `CONTEXT_WINDOW_EXCEEDED` 压缩路径；DeepInfer 最多恢复一次，只在持久 surface 有替换后重建请求。摘要选择历史的前半段，并沿既有工具调用/结果配对边界调整，避免摘要调用重复整份超限输入。摘要或重建请求仍超限则明确失败，原始 append-only 历史保留；没有可压缩完整区间时也不静默删消息。

验证运行 `python -m unittest discover -s tests -v`；设置 `DEEPINFER_COUNTER` 和 `DEEPINFER_TOKENIZER` 后，Node 预算测试还会运行真实原生计数。`tests/recovery.test.mjs` 通过 `DEEPINFER_RUNTIME_MODULES`、`DEEPINFER_SDK_CLIENT`、`DEEPINFER_PATCHED_DIR`、`DEEPINFER_TEST_ROOT` 指定独立环境，用实际 SDK profile 加本地 HTTP fixture 验证明确 400、仅一次恢复、失败后停止及工具配对。这些测试不代替真实 DeepInfer 请求验收。

维护责任归本配方维护者；DSH 升级必须重新生成和验收。当上游覆盖最终 payload 计数与受限恢复后，先验收上游替代，再删除 profile 里的 sidecar 行和本补丁，恢复原模块。回退只撤销本次 profile diff，不整份覆盖随后发生的用户配置变动，不修改服务端、认证或会话历史。

多 DP 下，重复前缀应路由到同一个 DP。功能 smoke 使用 loopback 后端的 `X-data-parallel-rank: 0`；实际多用户入口需要既有网关的稳定会话路由。不要用轮流分配请求的入口验证缓存复用。

`--enable-prompt-tokens-details` 让 API 用量携带缓存 token 数。dsh 底部是整会话累计且整数取整；旧请求缺失的用量不补记。应查看最新回答下的「用量」，不要把老会话累计 0% 直接判为缓存关闭。

## 功能验收和回退

1. `/health`、`/v1/models` 返回正确模型与总窗口；检查日志无 worker 初始化失败。
2. 运行 `scripts/smoke.py` 检查文本、SSE DONE 和缓存用量。该脚本只使用合成提示词，不记录时间或速率。
3. 图片数量上限为每请求含历史 640 张，图片、文本和输出合计须符合 1M 上下文，入口请求体限制独立生效。先前 512K 实例已用逐图编号的合成图验证 640 图、四轮历史累计和同一/多个 DP 的 4 请求并发；641 图及超上下文请求明确拒绝。1M 实例完成 16 图回归，未实测 640 图与接近 1M 文本的组合上限。未逐图编号时观察到指定序号定位错误，因此大量图片应在每张图前标注 `Image N:`；这些功能检查不代表任意图片的定位或识别质量保证。新机仍须重新验证图像内容、换图、重复图和历史图片。
4. 验证客户端文本、图片和工具调用；确认最终用量能显示缓存读取。源码或模型版本变化后还要重新做长上下文/输出边界检查。
5. 新机 `/metrics` 可经同一 SSH 隧道访问，例如 `http://127.0.0.1:19001/metrics`；检查实际8个DP的状态。各DP首次处理请求后才可能出现完整统计。
6. 保留旧容器和原配置，明确识别新旧实例再切换入口。若启用了[空闲恢复](recovery.md)，先停止其 timer/service，升级或回退完成后重新绑定目标并清空计时状态再启用。失败时仅停止本次新容器并恢复原入口；回退动作由操作者按目标机器归属执行，本仓脚本不自动停止其它服务。

这份核对单不等同于新机器已经验收。机器、镜像、CANN、模型 revision 或补丁任一变化，都需保存新的功能验收结论。
