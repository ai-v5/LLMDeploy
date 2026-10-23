# A2 / 910B1 / DeepInfer

状态：当前实施方向是双机16卡 EP16、全部专家常驻 HBM。固定模型源码已通过实际文本请求、MTP、前缀缓存和32K功能检查；公开双机入口已通过两端只读身份检查，并实际启动16个worker，通过短请求、MTP和缓存检查。**262144仅为窗口配置，未实测验收；后续 prefill 测试不超过32768，常规迭代使用较短输入。** 较早候选间的强制续写差异仍保留质量晋级待核项，不能把检索烟测、组件一致性或窗口配置当作完整质量与容量验收。

本方案使用公开的 [Eco-Tech ModelSlim W8A8 权重](https://www.modelscope.cn/models/Eco-Tech/DeepSeek-V4.1-Flash-1017-w8a8)，服务保持 P/D 不分离。它是独立的910B1路径，不沿用 [950DT配方](../../a5/950dt/deepinfer.md)的 native 库、设备清单或内存比例。原单机八卡 CPU 专家 offload 配方作为历史兼容记录保留，不是当前双机常驻专家部署入口。

## 双机 EP16 常驻专家候选

使用独立的 [EP16配方](../../../../recipes/a2/910b/deepinfer/deepseek-v4.1-flash-1017-ep16/recipe.json)、[源码补丁链](../../../../recipes/a2/910b/deepinfer/deepseek-v4.1-flash-1017-ep16/source-files.json)、[运行工件身份](../../../../recipes/a2/910b/deepinfer/deepseek-v4.1-flash-1017-ep16/runtime-lock.json)和[启动器](../../../../scripts/serve_deepinfer_ep16.py)，不要将八卡启动器用于此源码集合。

| 项目 | 双机固定配置 |
| --- | --- |
| 硬件与并行 | 两台 Linux aarch64，各8张64GiB的910B1；EP16 / DP16 / CP1 / ENP16 |
| 专家 | 主模型和MTP的 routed/shared expert 权重及scale均在所属NPU常驻；不使用CPU专家offload或热点缓存替代 |
| 其他主存使用 | Engram仍采用CPU选行读取，不能据此宣称整个模型无H2D传输 |
| MoE通信 | 当前为padding all_gather、紧凑本地专家计算和all_reduce；不是已实现的真AlltoAll dispatch |
| 调度与缓存 | eager、prefill budget2048、每rank最多1请求、memory fraction0.95；前缀缓存开启 |
| MTP | 保持5-token草稿和DSpark；独立MTP embedding与FP32 head不与主模型混用 |
| 软件身份 | CANN9.1.0 / Python3.12.13 / torch2.10.0+cpu / torch-npu2.10.0.post4；已用宿主驱动25.5.3，其他组合需另验 |
| 证据边界 | 固定模型源码的实际请求验收与公开入口自身的启动验收分开记录；配置262144不代表通过该长度测试 |

当前补丁含常驻专家、动态量化复用、紧凑专家计算、indexer地址/评分批处理和注意力临时内存复用。single-bank prefill按64个query分块，decode和多bank仍按8个query分块；QK、softmax、PV保持FP32计算。扩大prefill分块会增加该组件的临时显存，不能描述成节省整机HBM。

在新的源目录从固定commit开始，依次应用原910B补丁和EP16增量；先按`source-files.json`核对两个补丁的SHA256：

```sh
git clone https://github.com/ai-v5/deepinfer_ascend.git "$WORK_DIR/src/deepinfer-runtime"
git -C "$WORK_DIR/src/deepinfer-runtime" checkout --detach 07e5002c08bce137ab2a86a5a1fc8bb652729ad6
git -C "$WORK_DIR/src/deepinfer-runtime" apply --check "$DEPLOY_REPO/patches/deepinfer/a2-910b-modelslim-w8a8.patch"
git -C "$WORK_DIR/src/deepinfer-runtime" apply "$DEPLOY_REPO/patches/deepinfer/a2-910b-modelslim-w8a8.patch"
git -C "$WORK_DIR/src/deepinfer-runtime" apply --check "$DEPLOY_REPO/patches/deepinfer/a2-910b-ep16-resident.patch"
git -C "$WORK_DIR/src/deepinfer-runtime" apply "$DEPLOY_REPO/patches/deepinfer/a2-910b-ep16-resident.patch"
```

本仓只保存源码和身份清单，不分发权重、镜像或二进制工件。两端分别准备自己的工作目录及完整模型，保留工作目录内部的符号链接。除`src/deepinfer-runtime`外，还需匹配锁定文件的`envs/native-extra-site`、`envs/runtime-site`、`envs/gcc13`、`envs/v41-ops`和`build/vllm-bindings-r1`；不能直接换用历史八卡API venv。公开镜像tag只是来源说明，实际启动应核对配方中的完整镜像ID；tag漂移或重新构建都需要复验。模型大小检查不替代一次完整SHA256校验。

两端必须设置相同的`COORDINATOR_IP`、`WORKER_IP`和新的`RUN_ID`，分别选择`NODE_ROLE=coordinator`与`NODE_ROLE=worker`及本机的`GLOO_INTERFACE`。控制连接只用于受信任的两节点网络，不提供密码认证；`RUN_ID`是运行名称，不是认证secret。不要把控制端口暴露为公共入口。默认API只监听coordinator的loopback8900；控制端口29570，`MASTER_PORT=29550`对应原生实际store端口29500。

先在固定镜像和挂载下执行`--plan`或`--check-only`。两者都不启动模型；默认不传动作时也只检查。仓库挂载必须在工作目录容器边界内，例如`/workspace/deploy`：

```sh
python3 /workspace/deploy/scripts/serve_deepinfer_ep16.py --check-only \
  --role "$NODE_ROLE" --coordinator "$COORDINATOR_IP" --peer "$WORKER_IP" \
  --interface "$GLOO_INTERFACE" --run-id "$RUN_ID"
```

确认设备归属、两端8卡健康空闲、主存和端口后，按[通知维护流程](../../../notifications.md)处理通知。外部owner已暂停的任务不能由本次部署擅自恢复。以下命令只创建使用者选定的新容器，不停止已有服务；两端各执行一次，并提供自己的工作目录、模型路径和新的容器名：

```sh
mkdir -p "$WORK_DIR/home" "$WORK_DIR/tmp" "$WORK_DIR/cache/pyc"
NPU_SMI="$(readlink -f "$(command -v npu-smi)")"
IMAGE_ID=sha256:2ae3b99b6d3ba3162f152877357a2cc6e7124e3aed9b6a33c1bfd7da7d2def89
docker run --detach --name "$CONTAINER_NAME" \
  --network host --shm-size 8g --cap-add SYS_NICE \
  --env NODE_ROLE="$NODE_ROLE" --env COORDINATOR_IP="$COORDINATOR_IP" --env WORKER_IP="$WORKER_IP" \
  --env GLOO_INTERFACE="$GLOO_INTERFACE" --env RUN_ID="$RUN_ID" \
  --env HOME=/workspace/home --env TMPDIR=/workspace/tmp --env PYTHONPYCACHEPREFIX=/workspace/cache/pyc \
  --mount "type=bind,src=$WORK_DIR,dst=/workspace" \
  --mount "type=bind,src=$MODEL_DIR,dst=/model,readonly" \
  --mount "type=bind,src=$DEPLOY_REPO,dst=/workspace/deploy,readonly" \
  --mount type=bind,src=/usr/local/Ascend/driver,dst=/usr/local/Ascend/driver,readonly \
  --mount "type=bind,src=$NPU_SMI,dst=/usr/local/sbin/npu-smi,readonly" \
  --device /dev/davinci0 --device /dev/davinci1 --device /dev/davinci2 --device /dev/davinci3 \
  --device /dev/davinci4 --device /dev/davinci5 --device /dev/davinci6 --device /dev/davinci7 \
  --device /dev/davinci_manager --device /dev/devmm_svm --device /dev/hisi_hdc \
  --entrypoint /bin/bash "$IMAGE_ID" -lc \
  'source /usr/local/Ascend/ascend-toolkit/set_env.sh && exec python3 /workspace/deploy/scripts/serve_deepinfer_ep16.py --serve --role "$NODE_ROLE" --coordinator "$COORDINATOR_IP" --peer "$WORKER_IP" --interface "$GLOO_INTERFACE" --run-id "$RUN_ID"'
```

启动器核对308份Python源码、固定控制源码、包版本、native/算子/协议工件和模型文件；`--full-model-hash`可额外读取全部模型内容。只有`--serve`会检查空闲卡/NIC/端口、建立运行目录并启动本机进程。PID1 supervisor保证native worker具有存活的父进程，spawn重新导入不会再启动一套coordinator或agent。

等待`/health`同时满足`ready`和`workers=16`，再检查真实JSON/SSE、冷/缓存输出一致、`prompt_cache_hit_tokens>0`及本次原生`accepted_draft_tokens>0`。`receipts/<RUN_ID>-runtime-rank*.json`记录专家驻留、原生计数和allocator采样；这些计数可能包括最终截断的草稿，不能直接作为HTTP吞吐。新运行必须使用新`RUN_ID`，保留旧容器与收据，不自动清理。新组合的短请求成功不继承历史32K验收，更不继承未完成的256K验收。

## 历史单机八卡范围

权威配置见 [recipe.json](../../../../recipes/a2/910b/deepinfer/deepseek-v4.1-flash-1017/recipe.json)、[源码身份](../../../../recipes/a2/910b/deepinfer/deepseek-v4.1-flash-1017/source-files.json)和[运行库锁定](../../../../recipes/a2/910b/deepinfer/deepseek-v4.1-flash-1017/runtime-lock.json)。

| 项目 | 当前 profile |
| --- | --- |
| 硬件 | Linux aarch64，单机 8 × 910B1，单卡 64 GiB；其他 910B 型号需单独验证 |
| 并行 | DP8 / EP8 / CP1 / ENP8，每 rank 最多一个请求 |
| 权重 | revision `12b85455a58c7df345c1abc43934624a76bac576`；152 个文件内容哈希已与公开清单匹配 |
| 源码 | `ai-v5/deepinfer_ascend` commit `07e5002c08bce137ab2a86a5a1fc8bb652729ad6`，加固定 [910B 补丁](../../../../patches/deepinfer/a2-910b-modelslim-w8a8.patch) |
| 算子参考 | vLLM Ascend commit `5f828ad638ee4f9b12db87f9d5481f29076d37f9`，SOC `ascend910b1` |
| 容器软件 | Ubuntu 22.04.5 / glibc 2.35，CANN 9.1.0，Python 3.12.13，GCC 13.4.0，torch 2.10.0+cpu，torch-npu 2.10.0.post4 |
| 已用宿主组合 | 驱动 26.0.rc1、CANN 9.0；变更组合必须重新验收 |
| 调度 | eager，prefill budget 2048，memory fraction 0.75，EPLB 关闭；验收时每步记录统计 |
| MTP | 三层 DFlash，最多提出 5 个 token，DSpark 变长验证开启；保留独立 MTP embedding 与 FP32 输出 head |
| 缓存 | 原生前缀缓存开启；YOCO mask 保留 256 tokens，状态槽比例 2 |
| 容量与图片 | 总窗口配置 262144、最大输出 32768；图片接收上限配置 1，视觉推理未验收 |

W8A8 是混合精度导出：量化专家采用动态 INT8，其他参数保留其 checkpoint dtype。此路径使用 BF16 attention/indexer 和 CPU 专家 offload，不把 INT8 权重重新解释为 A5 FP8/FP4。当前启动预检要求至少 700 GiB 可用主存；这属于该固定 profile 的资源门槛，不是所有 W8A8 模型的通用最低值。

当前索引实现只对完整的 4096 个有效 key 分块合并收集和矩阵乘法；保留每个 query 的紧凑索引顺序、FP32 按 head 求和及尾块原计算路径，decode 索引路径保持不变。六组真机组件对照覆盖长 dense 索引、不同 query 的长候选块、重复块、填充和 NaN 遮罩；这不替代新源码的完整模型验收。

## 准备固定工件

本仓不分发镜像、权重或二进制依赖。镜像 ID 只标识已用运行环境，**没有对应的公开 registry 发布**；仅凭本仓不能拉取该镜像。换机时从持有已验证工件的一方取得私有镜像归档与运行工件包，保留符号链接，并将全部任务文件放在自己的工作目录。

容器内固定绑定为 `/workspace`，准备下列布局。`src/deepinfer-runtime` 应包含完整固定源码；`envs/api` 可以指向工作目录内的已锁定 API venv。native 扩展及 venv 的符号链接目标也必须随工件包保留。

```text
/workspace/src/deepinfer-runtime
/workspace/envs/api
/workspace/envs/python-site
/workspace/envs/gcc13
/workspace/envs/v41-ops
/workspace/build
```

源码也可从固定公开 commit 重建，再人工应用补丁。以下操作要求使用新的源目录，先核对补丁 SHA256 与配方一致：

```sh
git clone https://github.com/ai-v5/deepinfer_ascend.git "$WORK_DIR/src/deepinfer-runtime"
git -C "$WORK_DIR/src/deepinfer-runtime" checkout --detach 07e5002c08bce137ab2a86a5a1fc8bb652729ad6
git -C "$WORK_DIR/src/deepinfer-runtime" apply --check "$DEPLOY_REPO/patches/deepinfer/a2-910b-modelslim-w8a8.patch"
git -C "$WORK_DIR/src/deepinfer-runtime" apply "$DEPLOY_REPO/patches/deepinfer/a2-910b-modelslim-w8a8.patch"
```

这只重建 Python 源码。应用或转交补丁时保留本仓 AGPL-3.0-only 许可及[第三方声明](../../../../THIRD_PARTY_NOTICES.md)，同时保留上游源文件的许可与版权声明。重新编译 native 或自定义算子会产生需要独立验收的新工件，不能假定与已用二进制相同。构建基线为上述 DeepInfer/vLLM Ascend commits、CANN 9.1.0 和 GCC 13.4.0；native 使用 `setup.py build`，vLLM bindings 使用 CMake 的 `vllm_ascend_C` target。现有工件的源码、native、GCC runtime、自定义算子和 Rust recipe 二进制身份均在锁定文件中。

API 环境必须与 native 环境隔离；[API 依赖锁定](../../../../recipes/a2/910b/deepinfer/deepseek-v4.1-flash-1017/requirements-api.lock)列出固定版本，Rust 协议扩展还须匹配二进制哈希。不要直接复制整个历史工作目录中的日志、认证、HOME 或用户会话作为交付包。

权重校验必须显式选用 W8A8 清单：

```sh
python3 "$DEPLOY_REPO/scripts/verify_model.py" \
  --root "$MODEL_DIR" \
  --manifest "$DEPLOY_REPO/recipes/a2/910b/deepinfer/deepseek-v4.1-flash-1017/model-files.json" --full
```

完整校验读取全部权重。后面的默认启动器检查只核对模型文件大小，不替代这一步。

## 检查、命令审阅与显式启动

先核对设备归属、8 卡健康和空闲、主存、磁盘及端口，按[通知维护流程](../../../notifications.md)暂停通知与独立守护。不要停止其他人的容器。设置自己的 `WORK_DIR`、`MODEL_DIR`、`DEPLOY_REPO`，准备所需目录，并将私有归档通过 `docker image load` 导入。

在已具备固定运行工件的容器中，以下两个命令不会启动模型；`--plan` 只打印参数，`--check-only` 校验文件及包元数据：

```sh
/usr/local/python3.12.13/bin/python3 /deployment/scripts/serve_deepinfer_a2.py --plan
/usr/local/python3.12.13/bin/python3 /deployment/scripts/serve_deepinfer_a2.py --check-only
```

路径不采用默认布局时，可显式传入容器内的 `--source` 和 `--api-python`。二者必须位于 `/workspace`，API venv 必须使用固定镜像中的解释器。身份检查不导入 Torch，也不创建 NPU worker；通过不等于模型验收。

以下是公开候选容器命令。启动器已在具有上述固定运行工件的隔离容器中实际启动并通过短请求检查；新的容器和换机组合仍需独立验收。先人工阅读，确认工作目录及空闲资源后显式执行；它不覆盖或停止已有容器。`CONTAINER_NAME` 必须由使用者选择新的名称。

```sh
mkdir -p "$WORK_DIR/logs" "$WORK_DIR/home" "$WORK_DIR/tmp" "$WORK_DIR/cache/xdg" "$WORK_DIR/cache/hf"
docker run --detach --name "$CONTAINER_NAME" \
  --network host --ipc private --shm-size 64m --privileged \
  --env HOME=/workspace/home --env XDG_CACHE_HOME=/workspace/cache/xdg \
  --env HF_HOME=/workspace/cache/hf --env TMPDIR=/workspace/tmp \
  --mount "type=bind,src=$WORK_DIR,dst=/workspace" \
  --mount "type=bind,src=$MODEL_DIR,dst=/model,readonly" \
  --mount "type=bind,src=$DEPLOY_REPO,dst=/deployment,readonly" \
  --mount type=bind,src=/usr/local/Ascend/driver,dst=/usr/local/Ascend/driver,readonly \
  --mount type=bind,src=/usr/local/dcmi,dst=/usr/local/dcmi,readonly \
  --mount type=bind,src=/usr/local/bin/npu-smi,dst=/usr/local/bin/npu-smi,readonly \
  --mount type=bind,src=/etc/ascend_install.info,dst=/etc/ascend_install.info,readonly \
  --device /dev/davinci0 --device /dev/davinci1 --device /dev/davinci2 --device /dev/davinci3 \
  --device /dev/davinci4 --device /dev/davinci5 --device /dev/davinci6 --device /dev/davinci7 \
  --device /dev/davinci_manager --device /dev/devmm_svm --device /dev/hisi_hdc \
  --entrypoint /bin/bash \
  sha256:4d5b79f6f4075934b3676c8b4aa5c2bd429244b92e013b42d50dd166e713d343 \
  -lc 'source /usr/local/Ascend/ascend-toolkit/set_env.sh && exec /usr/local/python3.12.13/bin/python3 /deployment/scripts/serve_deepinfer_a2.py --serve >>/workspace/logs/api.log 2>&1'
```

启动前需创建工作目录内的 `logs/`。启动器检查固定源码与工件、设备身份及空闲、可用主存和端口，只在 `--serve` 时创建自己的 worker wrapper。API 默认仅监听 `127.0.0.1:8900`，内部 master 端口默认 29500；可用 `--port`、`--master-port` 显式调整。

## 验证、换机与回退

等待 `/health` 返回 `ready` 且 `workers=8`，检查 `/v1/models`，再做真实 JSON/SSE 查询、实际 MTP 接受和冷热前缀一致性检查。新源码已通过固定 32-token 输出请求、2223-token 输入的冷热请求及 2048-token 前缀命中，以及超出总窗口一个 token 的拒绝；它们仍属于短请求验收，不能提升为完整 256K 结论。当前配方设置 `DEEPINFER_LOGGING_INTERVAL_STEP=1`，需在实际请求期间核对 `verified_tokens` 和 `accepted_tokens` 正计数及长 prefill 进度。它们是滑动步骤窗口统计，不能直接相加当作请求总接受率。

历史单机未完成262144总窗口的真实完整请求验证；当前测试上限为32768，不安排更大的prefill。图片、P/D分离、图模式和其他硬件/镜像组合未因此获得验收。性能结果与原始运行收据保存在私有工作目录，不进入本仓；旧A5 MFU分母不用于910B性能结论。

健康与查询验证通过后恢复通知并保留去重状态。loopback API 不独立校验 key；对外接入时由使用者配置 [SSH 转发或认证网关](../../../gateway.md)，不要把原生端口直接作为公共入口。

换机保留镜像 ID、上述目录布局、源码/运行库锁定和模型 revision，重新完成本型号验证。回退前先排空本次服务，保全旧镜像、旧源码及容器；这里只处理自己创建的实例，不自动删除、抢占或重启其他服务。凭据、机器身份、原始日志、权重和二进制不进入公开仓库。
