# 推理性能基准

这是一套可直接运行的推理服务基准，用来在同一模型、同一套请求上比较不同引擎或不同配置的
**首 token 时间（TTFT）** 与 **每 token 时间（TPOT）**。用例、生成脚本和测量工具都在
`tools/bench/`，不依赖外部数据下载，也不包含任何真实机器地址、凭据或用户会话。

本仓按项目约定**不保存实测数字**：随仓发布的是用例、工具与记录格式，实测收据留在各自的私有目录。
这样公开内容不会变成对某次运行的性能承诺，读者用自己的机器复现得到的才是自己环境的数字。

## 测量口径

| 指标 | 定义 | 为什么要分开 |
| --- | --- | --- |
| TTFT | 请求发出到**第一个流式增量**（思考或正文）的时间 | 决定交互体感与 agent 首步等待 |
| TTFT(content) | 请求发出到**第一段正文**的时间 | 思考型模型的用户可见延迟比 TTFT 更晚 |
| TPOT | `(总时长 - TTFT) / (completion_tokens - 1)` | 只反映 decode 阶段，不把 prefill 混进逐 token 时间 |
| E2E tok/s | `completion_tokens / 总时长` | 端到端产出速度，含 prefill |

三条实现约定，避免最常见的读数错误：

1. **token 数取服务端 `usage`**，不按字符估算，所以不同引擎的输出长度差异不会污染 TPOT。
2. **TTFT 从发出请求算起**，包含排队与 prefill；想单独看 decode 就不要用 TTFT 比较引擎的 decode 能力。
3. **每例重复 3 次取中位数**，同时记录 min/max；单次值不作为结论。

流式增量里本仓同时接受 `reasoning` 与 `reasoning_content` 两种思考字段名，避免因引擎改名漏计首 token。

## 用例清单

用例是 JSON 文件，放在 `tools/bench/cases/`。图像与长上下文都由脚本生成，可离线复现。

| 类别 | 用例 | 输入 | 观察点 |
| --- | --- | --- | --- |
| 极短请求 | `chat-tiny` | 48 token | 调度与网络开销下限 |
| decode 密集 | `recite` | 66 token，输出约 2K | 持续解码速率 |
| 长文生成 | `longform` | 88 token，输出约 2.4K | 长输出下 TPOT 是否漂移 |
| 代码开发 | `code` | 152 token，输出约 1.8K | 代码类产出速度 |
| 代码理解与修改 | `code-review` | 575 token（含片段） | 缺陷定位与修复，思考占比较大 |
| 工具调用 | `tools` | 单工具 schema，488 token | agent 首个 tool_call 到达时间 |
| 多工具选择 | `tools-multi` | 三工具 schema，460 token | 工具增多后是否仍直接出动作 |
| 单图解读 | `slide` | 1280×720 渲染图，650 token | 视觉编码开销 |
| 缩略图 | `slide-thumb` | 512×288，245 token | 低分辨率视觉输入 |
| 多图对比 | `slide-multi` | 两图，1214 token | 视觉 token 叠加 |
| 结构化输出 | `json-strict` | JSON Schema 约束解码 | grammar 对 TTFT/TPOT 的影响 |
| 长上下文 | `needle-32k` | 约 32K 填充 + 中段检索 | prefill 规模对 TTFT |
| 长上下文 | `needle-128k` | 约 128K 填充 + 中段检索 | 更大的 prefill 规模 |
| Agent 单轮 | `agent-turn` | 约 67K 填充 + 约 400 token 输出 | 真实 agent 形态：长输入、中等输出 |
| 多轮缓存 | `multiturn` | 三轮共享前缀 | 前缀缓存对第 2、3 轮 TTFT 的收益 |

`needle-*` 的填充段落由 `bench.py` 内的中性文本生成，检索目标是虚构校准码，不含业务数据。
`needle-128k` 与 `agent-turn` 会占用较多 KV，**在共享或生产服务上应谨慎或跳过**。

填充长度从 token 目标反推字符数，而中英混合文本的字符/token 比例取决于 tokenizer：同一份
用例在不同引擎上可能偏离目标一两成。**用例名只是量级，真实长度以收据里的 `prompt_tokens`
为准**。

## 合成用例与真实负载的差距

基准里的短用例（几十到几百 token 输入）和线上的 agent 请求不是一回事。真实 agent 一轮
往往带着数万到数十万 token 的上下文进来，输入规模会明显改变每 token 时间：解码阶段每步都要
扫过全部 KV，上下文越长单步越慢。

因此**只跑短用例会高估实际负载下的解码速度**。评估一个要用于 agent 后端的部署时，至少用
`agent-turn` 这类长输入 + 中等输出的用例复测一次；如果知道线上请求的长度分布，按分布的
分位数取值构造几档更有意义。仓内 `agent-turn` 的输入量级刻意贴近常见 agent 单轮，但任何
固定用例都不能替代自己环境里的真实分布。

## 运行方法

```sh
# 生成幻灯片资产（只需一次，需要 Pillow）
python3 tools/bench/make_slide.py --variant main --out tools/bench/assets/slide-1280x720.png
python3 tools/bench/make_slide.py --variant alt  --out tools/bench/assets/slide-alt-1280x720.png
python3 tools/bench/make_slide.py --variant main --size 512x288 \
    --out tools/bench/assets/slide-512x288.png

# 测一个 OpenAI 兼容端点
python3 tools/bench/bench.py \
  --base-url http://127.0.0.1:8900/v1 \
  --model <served-model-name> \
  --engine-label my-engine \
  --config my-engine-config.json \
  --repeats 3 \
  --out record/my-engine.json
```

常用参数：

| 参数 | 用途 |
| --- | --- |
| `--only <id> ...` | 只跑指定用例，便于在共享服务上做小规模抽测 |
| `--concurrency N` | 额外跑一轮 N 并发，输出聚合 tok/s 与墙钟时间 |
| `--api-key-env NAME` | 从环境变量取 Bearer token；密钥不进命令行、不进收据 |
| `--config file.json` | 附上被测引擎的配置，随收据一起留档 |
| `--cache-bust` | 每次采样加唯一前缀，强制冷 prefill |
| `--warmup N` | 每例先跑 N 次丢弃，避开编译与预热成本 |

## 三个会让结论失真的坑

**前缀缓存。** 重复同一个 prompt 测到的是缓存命中，不是 prefill。实测中同一个长上下文用例
三次采样为 0.52 / 3.14 / 0.56 秒，取中位数会把 3.1 秒的冷 prefill 报成 0.56 秒。要测 prefill
就加 `--cache-bust`；要测缓存收益就故意不加，两者不可混在同一张表里。

**编译与预热。** 约束解码、图捕获、JIT 在首次请求上有额外成本。用 `--warmup N` 丢弃前 N 次，
否则首跑数字会被当成稳态。

**网络位置。** TTFT 含客户端到服务的传输时间。收据里的 `network_floor_ms` 是五次元数据读取的
中位数，代表这条链路的固有开销。本文档的两次实测曾出现一端 3 ms、另一端 125 ms 的网络基线差，
扣掉它之后最长用例的结论从"差一倍多"变成"基本持平"。比较两个部署在不同位置的引擎前，
先看这个字段。


## 记录什么配置

同一份权重在不同配置下结果差异很大，所以每次测量都要求附一份引擎配置。字段模板见
[tools/bench/config.example.json](../tools/bench/config.example.json)，运行方式：

```sh
python3 tools/bench/bench.py --base-url ... --model ... \
  --engine-label my-engine --config my-engine-config.json --out record/my-engine.json
```

配置会原样写进收据的 `engine.config`，和数字一一对应。建议至少包含：

| 分组 | 字段 |
| --- | --- |
| 身份 | 引擎名、镜像 ID 与创建时间、vLLM / vLLM-Ascend 版本、驱动与 CANN 版本 |
| 硬件 | 卡型与卡数、单机或多机、是否 P/D 分离 |
| 并行 | TP / DP / EP / PP / CP，是否 EP 专家并行 |
| 窗口与容量 | `max_model_len`、`max_num_batched_tokens`、`max_num_seqs`、`block_size` |
| 调度特性 | 前缀缓存、分块预填充、SWA 有界重放、MTP/投机解码、cudagraph 模式与捕获尺寸 |
| 显存 | 每 rank KV token 容量、可用 KV 显存、该窗口下的并发上限 |
| 请求口径 | `temperature`、`max_tokens`、重复次数、并发数 |

这些字段来自被测服务的启动参数，属于部署配方信息，可以随仓库公开；密钥、真实地址、账号与
原始日志不入库。

## 怎么读结果

- **TTFT 与 TPOT 指向不同的瓶颈**。TTFT 主要受排队、prefill 与视觉编码影响；TPOT 主要受
  decode 批处理效率、投机解码与图模式影响。一个引擎 TTFT 更低而 TPOT 更高是常见组合，
  不应简化成"谁更快"。
- **比较前先确认可比性**。单机 8 卡 P/D 公卡、多机 P/D 分离、不同窗口与不同 `max_num_seqs`
  属于不同服务形态；即使模型权重相同，也不能把两边的 TTFT/TPOT 直接当成引擎优劣。
- **共享服务上的数字带噪声**。他人并发会同时抬高排队与 TTFT；在生产端点抽测时应用 `--only`
  缩小范围，并在记录里写明"共享、非独占"。
- **长上下文用例的 TTFT 随输入近似线性增长**，用它确认 prefill 路径是否正常，而不是当容量承诺。
- **输出长度不同的两次运行不能比 E2E tok/s**，只能比 TPOT。

## 边界

本基准测的是单一请求路径的延迟与短时段吞吐，不覆盖：多用户混合负载下的 SLO、长时间稳定性、
缓存冷热切换、故障注入、取消与断流语义、以及真实业务输入分布。用例文本与图像都是合成的，
代表不了特定语料的 token 分布。任何数字都只在记录的配置与版本下成立，配置或版本变化需重测。
