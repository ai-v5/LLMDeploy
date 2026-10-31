# A5 / 950DT / DeepInfer

本方案整理历史使用成功的**单机 8 卡原生 DeepInfer + recipe API**。多个这样的后端组成独立副本池；新源码的单机、双机 P/D 和 EP16 原生路径另见[三拓扑指南](deepinfer-topologies.md)，不与历史 API profile 混用。仅分享使用经验，不作兼容性、稳定性、性能或生产保证；敏感信息不提供，见[免责声明](../../../../README.md)。

## 固定范围

参数与源码/镜像/native 身份以 [recipe.json](../../../../recipes/a5/950dt/deepinfer/deepseek-v4.1-flash-1017/recipe.json) 为准。`source` 和 `image` 保留历史运行身份；`source_build_candidate` 单独记录新的源码构建版本，不替换历史启动配方。

| 项目 | 历史 profile |
| --- | --- |
| 设备与并行 | Linux aarch64、单 OS、8 卡 A5 / 950DT；DP8 / EP8 / CP1，每 rank 最多 4 个请求 |
| 模型 | DeepSeek-V4.1-Flash-1017，revision `1bae0e1088993b9b82de08f59de5be5a34e42170` |
| 源码 | [deepinfer_ascend](https://github.com/ai-v5/deepinfer_ascend) commit `07e5002c08bce137ab2a86a5a1fc8bb652729ad6`；父适配版本及 vendor SHA 在配方内 |
| 软件 | CANN 9.2.0、Python 3.12.13、GCC 13.4.0、torch 2.13.0+cpu、torch-npu 2.13.0rc1 |
| API | 独立 venv，deepseek-recipe 0.2.0；chat completions / responses / messages，JSON/SSE |
| 容量 | 总窗口 1,048,576，默认及最大输出 32,768，图片接收上限 999，仍受总窗口约束 |
| 调度 | prefill budget 2048，prefill interval 64，EPLB interval 8192，memory fraction 0.95 |
| 推测与图 | NEXTN 最大 5、主图 interval 6；此 profile 使用 **`--no-dspark`，不是关闭所有 MTP** |
| 缓存 | DeepJIT、TileLang 持久目录；原生前缀缓存与 DP 调度由后端负责 |

deepseek-recipe 负责协议、模板和输出解析，实际计算仍在常驻 DeepInfer worker 中，不经过 vLLM。约束工具调用等功能边界以固定源码的 `patches/ascend/README.md` 为准，不把自动工具格式化当作所有约束模式都支持。

## 资源、权重与私有机器配置

先确认资源归属、8 卡健康且无任务、驱动/CANN/架构、主存/共享内存/磁盘及实际 RDMA 设备。不要停止其他人的容器或复制别机拓扑。0.95 内存比例不能直接套用 A2/A3。

同一模型 revision 共用既有清单，不重复维护权重哈希：

```sh
python3 scripts/verify_model.py --root /srv/models/DeepSeek-V4.1-Flash-1017 --full
mkdir -p local
cp hosts/a5/950dt/deepinfer.example.json local/deepinfer-host.json
```

在私有 JSON 中填写自己的路径、容器名、API/master 端口和已导入 image ID。模型只读，工件目录持久可写。`warmup_file` 指向自行创建的合成预热文件；示例不含认证。999 是接收上限，不保证任意 999 张图能放入上下文。

## 镜像迁移

配方记录历史 image ID，未发布 registry 镜像，仓库不提供二进制。由操作者在授权环境保存镜像，独立核对来源文件 SHA 后传输：

```sh
docker image save --output local/deepinfer-runtime.tar sha256:93b89c1ff22719cb91041f6a5b7d88323278e4e7a99812464a995094b70729ce
sha256sum local/deepinfer-runtime.tar
# 在目标机核对来源 SHA 后导入；不能只在目标机新算一个值代替来源验收。
docker image load --input local/deepinfer-runtime.tar
docker image inspect --format '{{.Id}}' sha256:93b89c1ff22719cb91041f6a5b7d88323278e4e7a99812464a995094b70729ce
```

导出前检查镜像层及配置中没有凭据、会话、私有数据和不允许分发的依赖；仅检查 Env 没有 key 不足以证明镜像可公开。上述命令不由仓库工具自动执行。

## 构建来源与边界

新的源码构建固定到 DeepInfer fork [PR #1](https://github.com/ai-v5/deepinfer_ascend/pull/1) 的合并提交 `b1203ac1bcc57df099d6e336255bf6a7b6e9bb83`，源码树为 `42a07a920e56cf35bb9704fa2818c8f57222f14b`。它包含 Ascend 兼容补丁、常驻 API、前缀缓存与预热、原生前缀 checkpoint、批量 worker 事件和默认关闭的 step 遥测。补丁及 vendor 的准确版本由该提交锁定，在 fork 的 `scripts/build_vendor.sh` 中应用，本仓不重复复制补丁。

此提交已通过 CPU 协议与源码回归。后续在与它源码树相同的构建提交上重建了 native，并完成三种拓扑的有限原生 NPU 推理；准确工件身份和限制见[三拓扑指南](deepinfer-topologies.md)。这不表示下面的公开源码 Dockerfile 已构建验收，也不表示新 API、完整上下文或所有跨拓扑输出一致性已验收。原生调度和绑定发生变化，必须配套构建 native 与 Python 源码，不能将新文件覆盖到历史 native 镜像中。

没有历史镜像时，可使用独立的[源码构建 Dockerfile](../../../../images/deepinfer/Dockerfile.source)。它获取 `source_build_candidate.commit`，从 Ascend 官方公开的 `ascendai/cann:9.2.0-beta.2-950-ubuntu22.04-py3.12-devel` 基础镜像开始，编译 GCC 13.4.0、固定 commit 的 vendor 库及 DeepInfer，并分别安装 native/API 环境，不依赖本项目未公开的父镜像。基础镜像来源见 [Ascend 镜像说明](https://hub.docker.com/r/ascendai/cann)及[官方 Dockerfile](https://github.com/Ascend/cann-container-image/tree/main/cann/9.2.0-beta.2-950-ubuntu22.04-py3.12-devel)。

在 Linux aarch64 构建机、LLMDeploy 仓库根目录执行以下命令；需要 Docker BuildKit、足够的内存/磁盘，以及访问公开镜像、系统软件源、源码与 Python 依赖源的网络：

```sh
docker build --platform linux/arm64 \
  --file images/deepinfer/Dockerfile.source \
  --build-arg MAX_JOBS=8 \
  --tag deepinfer-ascend:source-candidate .
```

`MAX_JOBS` 控制编译并发，内存不足时应降低。可通过 `CANN_BASE` 指定已核对的同款基础镜像 digest；默认 tag 不是不可变锁。该基础环境使用 CANN `9.2.0-beta.2`，官方 Python 为 `3.12.14`，与历史配方记录不同，不能据此宣称复现了历史镜像或性能。构建过程不复制本地源码、机器配置、凭据或权重，只从构建上下文读取公开的 API 依赖锁。默认命令仅显示 API 帮助，不自动启动模型。

**本次仅提供 Dockerfile，未执行镜像构建、真机验收或镜像发布。** 此新镜像不接入现有渲染器，历史 native SHA 校验保持不变；需要新配方、启动接入和端到端验收后才能作为已验证部署方案。以下 overlay 和镜像迁移仍仅适用于已有历史镜像的操作者。

### 历史 serving overlay

[serving overlay Dockerfile](../../../../images/deepinfer/Dockerfile) 依赖锁定的**已构建 native/API 父镜像**，不是从空 CANN 镜像构建。先导入配方的父镜像，然后从公开 commit 提取三处 profile 变更：

```sh
git clone --no-checkout https://github.com/ai-v5/deepinfer_ascend.git local/deepinfer-source
python3 scripts/prepare_deepinfer_image.py --source local/deepinfer-source --output local/deepinfer-build
docker build --network none --iidfile local/deepinfer-image.id local/deepinfer-build
```

准备工具只读取 `git show <固定commit>:<文件>`，不复制工作树、`.git`、本机配置或用户改动；输出目录必须不存在。Dockerfile 校验父镜像 native SHA 后复制固定公开源码。新 image ID 不保证等于历史值，须写入自己的机器配置并重新验收。入口校验源码 LF 归一化 SHA（历史镜像使用 CRLF）及 native SHA，不做 AST 宽松匹配。

没有该 native 父镜像时，使用上面的独立源码构建路线，不能将新 native 的 SHA 填成历史值。API 与 native 使用各自的 venv；历史版本锁保留在[配方目录](../../../../recipes/a5/950dt/deepinfer/deepseek-v4.1-flash-1017/)。新构建须记录依赖、源码、native 和 image 身份，运行 kernel、8 卡集体通信、完整模型/API 验收后，才能建立新的可启动配方。

## 新源码的实验性 P/D 入口

准确参数见固定提交的[实验性 P/D 指南](https://github.com/ai-v5/deepinfer_ascend/blob/b1203ac1bcc57df099d6e336255bf6a7b6e9bb83/examples/README.md#experimental-pd-checkpoints)。该路径使用可信共享目录传送不可变前缀 checkpoint，不是 HIXL 或 Mooncake 原生网络连接器。默认 `--pd-role combined` 仍是本地常驻推理。

P、D 两侧必须使用相同源码提交与匹配的 native 构建、精确 `--model-revision`、CP1、DP=EP 和兼容 NORMAL 布局。按相同模型固定 `--max-model-len 1048576 --max-output-tokens 32768`；请求输入与输出合计仍需遵守总窗口。`--max-num-seqs` 可按角色分别设置，容量差异不应改变布局语义；HTTP 并发或排队数不能当作原生 batch。

P 使用 `--pd-role prefill --pd-checkpoint-dir <私有共享目录>`，D 使用 `--pd-role decode --pd-checkpoint-dir <同一共享目录> --pd-prefill-url <P服务根URL>`，两侧均传 `--model-revision`。实际地址、目录、监听端口、两套 Python 和挂载由操作者配置。共享目录须已存在且仅可信 worker 可访问；内部认证通过私有环境变量 `DEEPINFER_PD_API_KEY` 提供，URL 不携带凭据。它不替代外部 API 的认证网关。

该参考路径仅支持文本，尚不支持多模态或结构化输出的 P/D 传输。D 校验模型、布局、token 指纹及文件完整性，分配自己的状态，恢复完整缓存块后计算剩余后缀；P 不提交采样输出。新构建仍须补跑原生 allocator 测试，以及同配置 full-D 与 P/D 的原生 token 一致性，成功 HTTP 响应不能代替这些检查。

`DEEPINFER_BATCH_TELEMETRY_DIR` 与 `DEEPINFER_BATCH_TELEMETRY_RUN_ID` 可选启用 rank/请求生命周期遥测，默认关闭。目录须预先存在，每轮使用新的 ID，原始记录保留在私有工件目录。step 时间不是 kernel 时间，跨机比较还需要时钟对齐。本仓不保存遥测记录或性能结果，此入口不接入已使用的服务池配方。

## 合成预热、生成与显式启动

把[短合成预热示例](../../../../recipes/a5/950dt/deepinfer/deepseek-v4.1-flash-1017/warmup.example.json)复制到自己的 `warmup_file`。它不包含历史会话，也不是私有预热的逐字复刻；应按自己的长输入、工具和并发形状补充合成样本。短预热不证明长上下文覆盖。

在目标 Linux 仓库目录执行：

```sh
python3 scripts/render_deepinfer.py --host local/deepinfer-host.json --output local/run-deepinfer.sh
bash -n local/run-deepinfer.sh
less local/run-deepinfer.sh
# 审阅并确认归属和资源后，再明确执行：
bash local/run-deepinfer.sh
```

生成器不运行 Docker、不覆盖输出、不停止旧容器。脚本先只读检查架构、8 卡健康及逐卡无进程、必要路径、镜像、容器名和 API/master 端口；采集失败/未知拒绝准入。检查不替代人工归属确认，也不能保证检查后资源永不变化。容器 `restart=no`，避免 Docker 在资源被回收后立即抢回；原生 API 只绑定 loopback。tini 管理主进程与 worker 生命周期，持久工件目录保留 JIT/TL 编译产物和日志。

## 验证、服务池与回退

```sh
curl --fail http://127.0.0.1:8900/health
curl --fail http://127.0.0.1:8900/v1/models
python3 scripts/smoke_deepinfer.py --base http://127.0.0.1:8900/v1
```

`/health` 必须等八 worker 初始化及预热完成。还需实际 JSON/SSE、reasoning、工具、图片、并发独立流、取消不影响其他流、取消后新请求和使用者客户端验收；不能只测健康。长输入另测实际用量、检索、边界及超限在 SSE 前拒绝。fork 的协议 fixture、配置存在、容器 running 或编译文件增长都不是模型/性能验收。

外部访问必须走[共享服务池](deepinfer-pool.md)的认证网关，原生 loopback API 不独立校验 key。滚动变更先按[网关排空](../../../gateway.md)关闭新请求，确认在途、原生请求与连接均归零，保全旧实例后由操作者切换或回退。失败时只处理此次新实例，不自动恢复或抢占他人资源。

验收后才按[DeepInfer 空闲恢复](../../../recovery.md#deepinfer-共享机器让机恢复)绑定精确容器/镜像/配置：动态串行采集、连续确认空闲至少两小时，人工 stop/kill 保持停止。人工维护先停恢复观察器；新容器重新绑定。通知与去重见[通知文档](../../../notifications.md)。

## 验证状态

此 profile 有历史真实八卡模型/API 使用记录，但不代表今天所有副本健康。后续新源码的双机原生推理记录见[三拓扑指南](deepinfer-topologies.md)，不能据此宣称双机在线 API 已验收。本次新渲染器、preflight、overlay 准备及导航只做本地验证，未重启、构建或启动真实服务；使用者仍需在独立空闲机器验收新的端到端启动流程。敏感配置、内部身份、原始日志及性能数据不提供。
