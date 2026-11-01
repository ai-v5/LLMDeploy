# 本机 token 统计

`deploy_usage` 是固定 vLLM 版本的 Stat Logger 插件；不增加网络监听，不依赖网关、Windows 或通知服务。每个 API 进程独立保存 SQLite WAL，按秒聚合其实际收到的引擎统计增量，查询时汇总所有 DP 和 API 进程。数据目录须挂载到宿主机并长期保留，不能随容器删除。

## 启用与查询

在既有渲染命令增加 `--usage`。生成器仍不执行部署；显式启动时注册本仓插件，写入宿主机 `artifacts_dir/usage`，代码和元数据通过 `PYTHONPATH` 加载，不需要联网或安装依赖。每次启动产生独立运行标识，默认配方期望 8 个 API 进程；手工改变 `--api-server-count` 时必须同步 `LLMDEPLOY_USAGE_WRITERS`。

```sh
python3 scripts/render.py --host local/host.json --output local/run.sh --usage
python3 -m deploy_usage.report --root /srv/artifacts/usage
```

启用需要重建容器，按通知文档先暂停告警，保留旧容器与配置，验证后恢复。固定模型、镜像和两个源码补丁不变。普通重启只产生新账本，不重置累计值；回退到未启用插件的容器期间不记账，会体现为未覆盖时间。

## 口径与边界

| 项目 | 定义 |
| --- | --- |
| 输入 | 与 `vllm:prompt_tokens_total` 相同，包含每次请求的历史上下文及图片 token；在引擎前端收到相应 prefill 统计时计入 |
| 输出 | 与 `vllm:generation_tokens_total` 相同，包含推理过程、首 token 和中止前已观测到的输出，不保证客户端收到 |
| 缓存 | 与 `vllm:prompt_tokens_cached_total` 相同，前缀 KV 命中与外部 KV 传输之和；已包含在输入中，不是 Engram 或图片预处理缓存 |
| 均速 | 窗口输出 / 窗口墙钟秒数，包含空闲及未覆盖时间；今天为零点至查询时刻，累计为首次记账至查询时刻 |
| 生成速度 | 已结束请求的 `sum(max(output-1,0)) / sum(decode_seconds)`，是按解码时间加权的请求速度，不能当作并发总吞吐；请求跨日时该项归入结束所在窗口 |

今天按北京时间零点切分；24 小时、3 天、7 天分别为滚动 24/72/168 小时。输入、输出及缓存按统计事件时间归属，边界精度 1 秒；报表不含尚未结束的当前秒。累计覆盖所有保留的账本。历史请求、原始 token、回答正文、请求 ID 和凭据均不保存。

回调只向容量为 4096 的队列非阻塞入队，独立线程写盘并每约 5 秒更新心跳。心跳只证明记录器在对应时间段工作。缺失 API 记录器、心跳超过 30 秒、队满、数据库损坏和写入失败均不能宣称完整；查询显示起点、截止时间和未覆盖时长，未知不当作零。记账故障不向推理抛出异常；数据丢失或损坏时应保留现状并恢复存储，不能删除账本后沿用旧累计。

SQLite 使用 WAL / NORMAL；已提交记录可跨普通进程退出保留，正常退出限时排空队列，异常退出可能丢失队列尾部，断电还可能丢失最近事务。引擎产生但前端尚未消费的输出，也可能遗漏。因此这是持续运行的用量统计，**不是可保证故障下逐 token 零丢失的计费账本**，也无法补齐启用前的历史。升级需要重新核验统计接口、与 Prometheus 增量一致性、8 个记录器及存储失败行为。

## 小鲁班状态

在私有通知配置的 `usage` 对象中，以既有节点端口为键配置 `target`（已有 SSH 信任的目标）、`code_dir`（本仓代码所在目录）、`data_dir`（宿主机账本目录）。示例结构：

```json
{"usage":{"19001":{"target":"example-primary","code_dir":"/srv/LLMDeploy","data_dir":"/srv/artifacts/usage"},"19002":{"target":"example-backup","code_dir":"/srv/LLMDeploy","data_dir":"/srv/artifacts/usage"}}}
```

通知进程每约 30 秒在后台通过 SSH 只读查询，两台分别超时，查询不会阻塞健康观察。仅允许固定报告命令、已验证的路径与目标；远端结果仅提取有界数字，原始文本和异常不进入消息。以通知机本地接收时间判断缓存是否超过 90 秒，避免跨机时钟偏差造成误判。事件与窗口仍按部署机时间；与通知机的估计偏差超过 5 秒时显示提示，不自动修改系统时钟。

`@小鲁班 状态` 保留部署数量和健康状态，并按实例返回五个窗口的输入、输出、缓存、命中率、均速和生成速度。IP 仍只显示最后一段；查询游标、投递去重及失败不自动重发的合同不变。统计故障不等同于 vLLM 故障。

## 统一入口的 IP 用量与本机页面

`deploy_stats` 是独立的入口统计组件，适用于固定 DeepInfer recipe API 的 Chat Completions、Responses 和 Messages JSON/SSE。它放在 Caddy 与**现有认证网关之间**，透传请求和响应，从正常结束的原生 `usage` 提取输入、输出及缓存命中；不会改变模型服务、认证或路由。页面和采集代理都只监听 `127.0.0.1`。这是与上面的 vLLM Stat Logger 分开的账本，不能把两者直接相加，也不自动补齐启用前的历史。

Chat Completions 的最终流式用量语义来自[固定版本的 recipe 源码](https://github.com/deepseek-ai/deepseek-recipe/blob/935817c66f5d6e64838f419692545f06787e485f/deepseek-recipe/src/protocol/openai/chat_completion/response/chunk_generator.rs)；更换后端或协议版本时须重新核对，不按旧版本假设填零。

页面展示今天、滚动 24 小时、7 天和累计的总 Token、输入、输出、缓存命中、窗口输出 tok/s 与各请求 IP；另有最近 24 小时和近 14 日的统计。**总 Token = 输入 + 输出**，缓存命中是输入子集。窗口输出 tok/s 为已计量输出 / 窗口墙钟秒数，包含空闲时间，不是单请求解码速度。所有 Token 按请求完成时间入桶；24 小时曲线显示完整的 10 分钟区间，未满区间仍计入概览。这不是逐 token 生成时刻的曲线，长请求在结束前没有最终用量。

请求中断、上游错误、未知协议、缺失或互相矛盾的用量字段记为未计量，不当成零。无采集覆盖显示空缺；`≥` 表示已观测下限，命中率仅针对已计量请求。队列满时记录丢失数并将相关采集运行周期视为不完整；运行周期与心跳用于标识中断，队列丢失数可能包含所选窗口以外的时间。SQLite 使用 WAL / NORMAL，异常退出或断电可能丢失尾部；这不是故障下逐 token 零丢失的计费账本。

页面的运行覆盖只反映采集进程心跳，不证明 Caddy 已经切换或所有入口流量均经过该代理。启动代理但尚未切换、入口配置后来变化或客户端绕过入口时，仍可能有未记录流量；必须单独核验真实入口路由，不能将心跳 100% 当作入口完整性证明。

为适配 Windows 的本机异步事件循环，采集代理最多同时处理 192 个连接；超限返回 429，并标记统计缺口。对于带请求体的请求，代理最多先等待约 10ms，以便透传上游立即返回的认证或准入拒绝，再开始转发请求体。上线前必须核对连接上限不低于实际网关需要的并发与等待配额，并实测对首 token 延迟的影响；扩大服务池后需重新评估，不能把连接数当作模型吞吐能力。

账本只保留 IP、请求结束时间、耗时、HTTP 状态及数值用量，不保存认证信息、提示词、模型回答或原始 Token。IP 和账本属于私有运行数据，目录需限制本机访问，只放忽略的 `local/` 或操作者自己的私有位置。页面拒绝跨站 Origin 和非 loopback Host，且不提供修改接口；同一台机器上有权限读取账本的用户仍可读取这些 IP。

下面是**待单独授权部署**时的示例；目录、端口和原网关地址须先核实，命令不会由开发或测试自动执行：

```powershell
New-Item -ItemType Directory -Path local/stats -Force
python -m deploy_stats proxy --port 19882 --upstream-port 19010 --database local/stats/usage.sqlite3
python -m deploy_stats dashboard --port 19881 --database local/stats/usage.sqlite3
```

示例中的 `19010` 仅代表既有网关的 loopback 端口。[Caddy 示例](../services/deepinfer/Caddyfile.stats.example)仅供审阅。先在隔离环境验证代理，再按维护流程排空入口，并将 Caddy 的 `reverse_proxy` 目标改为 `127.0.0.1:19882`，同时在该反代块内设置：

```caddyfile
header_up X-LLMDeploy-Client-IP {remote_host}
```

Caddy 必须覆盖客户端传入的同名头；采集器不信任客户端的 `X-Forwarded-For`，缺失、重复或非法来源显示为“未知来源”。`{remote_host}` 是 Caddy 的直接对端：若 Caddy 前还有其他代理，显示的是该代理 IP，需先明确可信代理链，不能自行相信转发头。参见 Caddy 的[反向代理头部说明](https://caddyserver.com/docs/caddyfile/directives/reverse_proxy#headers)和[请求占位符](https://caddyserver.com/docs/caddyfile/concepts#placeholders)。所有入口别名必须指向同一个采集代理，绕过它的流量不在统计范围。

切换前保留原 Caddy 配置，验证认证失败控制、JSON/SSE 字节内容、并发、取消、大请求、来源 IP 和页面；核对源版本与真实用量字段，检查记录器心跳及缺失数。验证通过才恢复通知。回退时把 Caddy 的目标恢复到原网关，保留账本并停止本次采集/页面进程；回退期间形成统计空缺，不得视为零。上述均需另行授权，离线测试和合成预览不构成实际部署或真机验收。
