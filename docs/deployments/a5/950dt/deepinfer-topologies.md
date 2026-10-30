# DeepInfer 原生部署：单机、P/D 分离与 EP16

这里补充同一份新源码在三种拓扑上的部署经验：单机八卡 P/D 混部、两台八卡机器分别做 P 与 D，以及两台机器组成一个 EP16 混部实例。三种拓扑都做过有限范围的原生模型推理；这不等于双机在线 API、完整上下文或所有输入都已验收。

本文只公开部署方法、工件身份和功能边界。性能结果、容量测试结果、曲线、原始收据和机器身份不公开。历史单机 API 仍按[原指南](deepinfer.md)使用，多副本服务池仍按[服务池指南](deepinfer-pool.md)操作。

## 三种拓扑怎么区分

| 方案 | 物理资源 | 引擎与 rank | 当前用途和边界 |
| --- | --- | --- | --- |
| 8P8D 单机 | 一台、共 8 卡 | 一个 DP8 / EP8 / CP1 引擎，rank 0–7 | 每张卡同时参与 prefill 与 decode；不是 16 卡 |
| 8P＋8D 分离 | 两台、共 16 卡 | P、D 各一个独立 DP8 / EP8 / CP1 引擎，各自 rank 0–7 | 文本前缀 checkpoint 参考路径；不是跨机 EP16 |
| 16 卡混部 | 两台、共 16 卡 | 一个 DP16 / EP16 / CP1 引擎；节点 0 为 rank 0–7，节点 1 为 rank 8–15 | 所有卡参与 P/D；需要跨机 collective 和分布式请求控制 |

多个独立八卡副本后面加负载均衡，不会变成上述后两种拓扑。双机首先核实处于同一个物理 64 卡 pod；只看一个较大的 fabric / Super Pod ID 不足以确认物理归属。实际 pod 编号、节点地址、网卡名和端口由操作者在私有配置中填写。

## 锁定源码与运行工件

[native-topologies.json](../../../../recipes/a5/950dt/deepinfer/deepseek-v4.1-flash-1017/native-topologies.json)保存本次不含性能数据的部署记录。它是描述性锁定文件，**不是**现有 `render_deepinfer.py` 的输入；不要用它替换历史 `recipe.json`。

| 身份 | 固定值 |
| --- | --- |
| 公开源码 | `ai-v5/deepinfer_ascend`，commit `b1203ac1bcc57df099d6e336255bf6a7b6e9bb83` |
| 实际 native 构建提交 | `7429712e0bd31418bba121bc9b152058579e1894` |
| 两个提交的共同源码树 | `42a07a920e56cf35bb9704fa2818c8f57222f14b` |
| 模型 revision | `1bae0e1088993b9b82de08f59de5be5a34e42170` |
| 环境镜像 ID | `sha256:b65fbd0353e4036be0f24019623ade6dd8bb65108ed85c6290de1df9110deeb0` |
| 另行构建的 native SHA256 | `5ade8bf73b6193107461493a336971c1399e1d4118aaf13cbbc5395d08d4f69c` |

两个提交的源码树相同，native 已在该源码树上重新构建并参与有限范围的 NPU 推理。运行时源码和 native 是另行只读挂载的，**镜像 ID 单独不能代表完整运行版本**。双机必须核对同一份 Python 源码、native、模型 revision、依赖和驱动库；驱动文件哈希见锁定文件。

该镜像未发布，也不随仓库分发。新环境可参考[源码构建路线](deepinfer.md#构建来源与边界)，但公开 `Dockerfile.source` 本身尚未完成构建和硬件验收；它使用的基础环境也不能冒充这里的历史运行工件。重新构建后记录自己的 image/native SHA，并重新验收。不能把历史 native SHA 填给新的二进制，也不能把新 Python 代码直接覆盖到旧 native 上。

## 权重、缓存与容器准备

同 revision 的权重可以放在两台都已挂载的共享文件系统上，容器内统一映射为只读 `/model`。共享存储减少重复分发，但不免除每台的加载、网络与资源检查。先用[共用模型清单](../../../../recipes/deepseek-v4.1-flash-1017/model-files.json)核对来源，再在目标机校验；不要修改全局挂载或原始权重。

```sh
python3 scripts/verify_model.py --root /srv/models/DeepSeek-V4.1-Flash-1017 --full
```

运行源码和 native 统一只读映射到 `/runtime`；每个节点、每个实例有自己的可写 `/artifacts`。DeepJIT 与 TileLang 缓存放在持久目录，按源码、编译器、设备和依赖身份隔离；缓存命中不能替代模型加载与实际工作负载预热。checkpoint 单独放在仅可信 worker 可访问的共享目录，不能和模型目录混用。

设备清单沿用[单机指南](deepinfer.md#资源权重与私有机器配置)逐机核对，包括八卡设备、管理设备和实际 UB/RDMA 设备；双机额外核对跨机网络、拓扑和 collective。不要照抄别机的接口名。host network、共享内存、主存与 memlock 限额均按资源许可设置，避免容器无限占用宿主机。新实例使用 `restart=no`，发生资源竞争只让出自己的实例。

## 共同引擎配置

本次原生路径使用 `NORMAL` KV 布局、CP1、DP=EP，固定最大 MTP 草稿数 5，关闭 adaptive DSpark。`--no-dspark` 关闭的是可变长度验证，**并未关闭 MTP**。

| 配置 | 值与含义 |
| --- | --- |
| `max_model_len` / `max_output_tokens` | `1048576` / `32768`；前者为输入与输出合计上限，配置存在不代表满窗口已实测 |
| `max_num_batched_tokens` | `2048`，单次 prefill token budget |
| `prefill_any_interval` | `1`，与历史在线 API 的 interval 64 不同，不能混用验收结论 |
| `eplb_interval` / `eplb_after_prefill` | `8192` / `False` |
| `DEEPINFER_USE_NEXTN_DECODE` | `5` |
| `DEEPINFER_CUDAGRAPH_BATCH_SIZE_INTERVAL` | `6` |

`max_num_seqs` 是每个 DP worker 的准入容量，不是已经达到的 native BS；`gpu_memory_utilization` 要连同权重、状态槽、图、workspace 与实际 KV 余量一起核算。这两项不提供跨拓扑通用值，更不能把所有请求都当作可同时填满上下文。遇到初始化内存不足、KV 容量不足或 OOM，保留私有失败收据并停止该轮后续档位，不自动调参重试。

## 单机与 P/D 的公开 API 入口

固定源码中的 `examples/serve_recipe.py` 默认创建本机 DP8 / EP8 的常驻 worker。原生 worker 使用 native Python，HTTP 使用 API Python；不要增加多个 HTTP worker 来复用同一批 NPU，否则会重复拉起模型。

在已授权、已准备且空闲的 Linux aarch64 容器中，可按下面的**新环境验收模板**启动单机。路径为示例，先准备自己的合成 warmup；本次只验证了原生拓扑，没有执行这一模板的在线 API 验收：

```sh
export DEEPINFER_ACCELERATOR=ascend
export PYTHONPATH=/runtime:/runtime/examples
export DEEPINFER_USE_NEXTN_DECODE=5
export DEEPINFER_CUDAGRAPH_BATCH_SIZE_INTERVAL=6
export DJ_JIT_CACHE_DIR=/artifacts/compiled-cache
export TILELANG_CACHE_DIR=/artifacts/tilelang-cache
export MASTER_ADDR=127.0.0.1 MASTER_PORT=29500

/opt/deepinfer-api-venv/bin/python /runtime/examples/serve_recipe.py \
  --model /model --worker-python /opt/deepinfer-venv/bin/python \
  --model-revision 1bae0e1088993b9b82de08f59de5be5a34e42170 \
  --max-model-len 1048576 --max-output-tokens 32768 \
  --max-num-batched-tokens 2048 --max-num-seqs 4 \
  --gpu-memory-utilization 0.95 \
  --prefill-any-interval 1 --eplb-interval 8192 --no-dspark \
  --warmup-workload /config/warmup.json --host 127.0.0.1 --port 8900
```

这里的容量 4、内存比例 0.95 是启动模板选择，不能作为最大容量或新 API 已通过验收的证明。检查完整源码/native 身份后再执行；不要交给仍校验历史 native SHA 的渲染器启动。

P/D 使用同一套共同参数和两套独立的八 rank 集合，再增加角色参数：

| 角色 | 新增参数 |
| --- | --- |
| P | `--pd-role prefill --pd-checkpoint-dir /shared/checkpoints` |
| D | `--pd-role decode --pd-checkpoint-dir /shared/checkpoints --pd-prefill-url "$PREFILL_ROOT_URL"` |

两侧 `MASTER_ADDR` 都为各自 loopback，master 端口在各自主机内唯一；这两个 EP8 集合不共用一个 EP16 rendezvous。`PREFILL_ROOT_URL` 在私有运行环境填写为 P 的根 URL，不含用户信息、key、query 或 fragment。P 的 `--host` 必须是 D 可达的私有监听地址，示例的 loopback 默认值不能直接跨机使用。

P、D 都从私有进程环境取得同一个 `DEEPINFER_PD_API_KEY`，不要放到命令参数、公开 JSON 或日志。P 的内部入口也要认证；对外 D 服务另经[认证网关](deepinfer-pool.md)，不暴露未保护的原生端口。

P 导出最后完整缓存块，D 校验模型、布局、token 指纹和文件完整性后恢复，随后计算未覆盖的输入后缀并采样。该路径通过 CPU 序列化及可信共享文件系统搬运 checkpoint，**没有实现 HIXL/Mooncake/RDMA KV 连接器**；UB/collective 能工作并不说明 checkpoint 使用了 RDMA。当前仅支持文本，不支持多模态或结构化输出传输。跨拓扑输出一致性尚未全面闭合，不能据有限原生用例把两种服务视为可互换。

## EP16：需要怎样的启动器

16 卡混部是一个引擎，不是在两台机器上各启动一个默认 `serve_recipe.py`。该 API CLI 没有分布式节点参数，其 `EnginePool` 会在本机启动所有 `config.dp` worker；仅设置 `WORLD_SIZE=2` 不会把它变成双机 API。

已运行的原生路径由任务级控制器分别在两机启动八个 worker，配置 `EngineConfig(ep=16, dp=16, pd_role="combined", ...)`。节点 0 只启动全局 rank 0–7，节点 1 只启动 rank 8–15；worker 内部的 `RANK` 为全局 rank、`WORLD_SIZE=16`。两机使用相同 `MASTER_ADDR` 与空闲 master 端口，`MASTER_ADDR` 指向节点 0 可达地址，网卡根据实际网络指定。rank 各自接收请求并回传事件，控制器负责跨机 ready、取消、错误和完整退出。

本仓不公开私有实验控制器，也没有交付分布式在线 API 启动器。若只做原生短用例复核，可参考固定源码的[离线多机启动器](https://github.com/ai-v5/deepinfer_ascend/blob/b1203ac1bcc57df099d6e336255bf6a7b6e9bb83/examples/_launch.py)和 `offline_inference.py --ep 16 --dp 16 --cp 1`。那里父启动器的 `WORLD_SIZE=2`、`RANK=0/1` 表示节点数与节点序号，区别于上面的 worker 环境；其默认长度、采样和调度也不同，不能用短用例冒充本页共同配置或 1M 验收。

## 上线前验收与资源让出

先核实资源许可、八卡健康、逐卡 HBM/进程、外来训练或推理父进程、驱动和工件身份；空结果、超时或 UNKNOWN 不能当空闲。双机先通过全 rank collective，再加载模型、预热预期输入/输出和并发形状。容器 running、缓存文件增长、单个 rank ready 都不等于服务恢复。

原生检查至少包含完整输入/输出 token、MTP 采样计数、所有 rank 参与、checkpoint 还原及与本地 full-decode 的一致性。API 还需另测 JSON/SSE、认证拒绝、取消及取消后新请求、缓存复用、上下文边界、worker 故障关闭准入与进程退出。原生成功不能代替这些 API 验收。

如需接入已有服务池，另行完成在线 API 验收、精确实例绑定和滚动排空；本文不改动任何既有入口。维护按[通知文档](../../../notifications.md)暂停和恢复对应通知守护，保留去重状态。故障恢复按[空闲恢复规则](../../../recovery.md#deepinfer-共享机器让机恢复)每分钟串行采集，连续确认空闲至少两小时；有人占用或采样未知时不重拉争抢。

以上内容依据本地构建、模型推理和退出验收记录整理，仅保留部署层结论。公开记录不包含当前机器可用性、可承载请求数、任何速度或性能对比，也不保证重新构建后沿用旧验收结论。
