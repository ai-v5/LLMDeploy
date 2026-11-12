# A5 P/D runtime 构建候选

状态：`PREPARATION_NOT_REBUILT_NOT_NPU`。这是依据既有 CPU 构建、安装和导入流程整理的独立候选；本次未重新执行 Docker、AOT 编译、模型加载或推理。原 [P/D 配方](../recipes/deepseek-v4.1-flash-1017-pd/recipe.json) 及其镜像身份保持不变。

## 输入与来源边界

[独立依赖锁](../build/pd-runtime-candidate/dependencies.json) 固定以下输入：

- 官方 `v0.30.0rc2-a5` 的 arm64 基础镜像 digest、config image ID、vLLM/Ascend commit 和 CANN 版本。
- CANNBotDSL 算子源码 commit `4025ade4a1000053fe38bba192d70ce5fdba835b`，用 `git -c core.autocrlf=false archive` 导出规范 LF 字节；不使用 checkout 的未提交修改。
- PyPI 的 `cannbot-dsl==0.7.1` CPython 3.12/aarch64 wheel，其公开 [release metadata](https://pypi.org/pypi/cannbot-dsl/0.7.1/json) 列出的文件名和 SHA 与锁一致。

精确源码 commit 已通过无仓库认证的独立 fetch 核实，`LICENSE` 的 LF 字节 SHA 也已核对。公开交付仍保留 `HOLD_EXACT_DIFF_AND_HISTORY_DELIVERY_REVIEW`；本候选未重建、未做 NPU 验证。仓库不附带 vendor 源码、wheel、编译结果、认证或代理配置。

旧构建输入 archive `2300b3ac...` 含 CRLF 文本。本候选使用 LF archive `6af85d77...`（14,090,240 字节），完整 SHA 均保留在独立锁中。两者来自同一 commit/tree，路径、模式和链接一致；525 个文件的字节差异全部是 CRLF/LF，不能将它称为仅 tar header 差异。旧输入身份仅作历史记录；本候选不声称逐位复现旧 wheel 或 overlay。

源码及 framework wheel 声明 CANN Open Software License Agreement Version 2.0；其 Huawei AI 处理器用途限制和通知保留要求继续适用。构建包装代码的 AGPL 不改变上游许可。生成 runtime 时从所锁源码带入完整 `LICENSE`，保留 wheel 自带的许可与第三方通知。这里没有发布或授权分发 vendor 二进制；后续分发仍须按上游许可核验。

## 单独准备依赖

以下命令供另行获准的 Linux arm64 构建环境使用，本文不执行它们。选用新的空目录；不要复用、删除或覆盖已有构建结果。下载和 Git 认证由操作者在仓外配置。

```sh
set -eu
mkdir downloads/pd-runtime-candidate
git clone https://gitcode.com/cann/cannbot-dsl downloads/pd-runtime-candidate/source
git -C downloads/pd-runtime-candidate/source rev-parse --verify 4025ade4a1000053fe38bba192d70ce5fdba835b^{commit}
git -C downloads/pd-runtime-candidate/source -c core.autocrlf=false archive --format=tar \
  --output="$PWD/downloads/pd-runtime-candidate/cannbot-source-4025ade4.tar" \
  4025ade4a1000053fe38bba192d70ce5fdba835b
python3 -m pip download --no-deps --only-binary=:all: \
  --python-version 312 --implementation cp --abi cp312 --platform manylinux_2_28_aarch64 \
  --dest downloads/pd-runtime-candidate cannbot-dsl==0.7.1
python3 build/pd-runtime-candidate/verify_runtime.py --check-source-archive \
  downloads/pd-runtime-candidate/cannbot-source-4025ade4.tar
```

先核对依赖锁中的 archive、其内部 `LICENSE` 和 framework wheel SHA。检查器要求原始字节一致，不静默转换换行。单独 archive/license 检查通过只证明输入身份。上游源码 commit 是算子来源；不据此声称 PyPI framework wheel 由相同 commit 编译。

## 复用已实际使用的三算子构建入口

复用 [build_ops.sh](../build/build_ops.sh)，不复制或改写 vendor builder。该包装脚本的 SHA 在独立锁中固定；只将它读取的依赖元数据覆盖挂载为新锁。旧 [dependencies.json](../build/dependencies.json) 和旧镜像文档仍服务其原配方，不修改。本候选强制提供已校验的规范 tar，并仅挂入 tar 和 wheel；不挂入 `/deps/source`，不走包装脚本的 source-only fallback。另行复用旧 fallback 也须先按上文显式关闭 EOL 转换生成并校验 tar，不能依赖构建环境的 Git 配置。

```sh
set -eu
python3 build/pd-runtime-candidate/verify_runtime.py --check-source-archive \
  downloads/pd-runtime-candidate/cannbot-source-4025ade4.tar
docker pull --platform linux/arm64 \
  quay.io/ascend/vllm-ascend@sha256:10f4e41479604545b74f3684e1a98e9e7116eff6adc72ecaec765d2dbecc7e81
docker image inspect \
  quay.io/ascend/vllm-ascend@sha256:10f4e41479604545b74f3684e1a98e9e7116eff6adc72ecaec765d2dbecc7e81 \
  --format '{{.Id}}'
# 必须等于独立锁中的 config_image_id。
mkdir local/pd-ops-build
mkdir local/pd-ops-build/home
docker run --name pd-ops-build --network none --cpus 16 --memory 32g \
  -v "$PWD:/deployment:ro" \
  -v "$PWD/build/pd-runtime-candidate/dependencies.json:/deployment/build/dependencies.json:ro" \
  -v "$PWD/downloads/pd-runtime-candidate/cannbot-source-4025ade4.tar:/deps/cannbot-source-4025ade4.tar:ro" \
  -v "$PWD/downloads/pd-runtime-candidate/cannbot_dsl-0.7.1-cp312-cp312-manylinux_2_28_aarch64.whl:/deps/cannbot_dsl-0.7.1-cp312-cp312-manylinux_2_28_aarch64.whl:ro" \
  -v "$PWD/local/pd-ops-build:/build" \
  -e ASCEND_TOOLKIT_HOME=/usr/local/Ascend/ascend-toolkit \
  -e HOME=/build/home -e OMP_NUM_THREADS=1 -e CARGO_BUILD_JOBS=16 -e MAX_JOBS=16 \
  --entrypoint /bin/bash \
  sha256:c3d00a196905159dbf0b97588d34f085086c2708712fa0d1c8fedd807f0e48a4 \
  --noprofile --norc /deployment/build/build_ops.sh
docker inspect pd-ops-build --format '{{.State.ExitCode}}'
```

此构建不映射 NPU，也不挂入模型。必须得到退出码 0 和 `local/pd-ops-build/result.json` 的 `PASS_BUILD_AND_IMPORT_ONLY`。原生入口实际为：

```sh
python3 /build/source/net/native_package/build.py \
  --operator mqsmla qli qsli --target dav-3510 \
  --output-root /build/output --work-root /build/work
```

不加 profile 过滤器，使用所锁源码与 framework 的默认 AOT profile 集合：MQSMLA 36、QLI 18、QSLI 18，共 72；另有 3 个 AICPU metadata 库。不能把参数笛卡尔积的未去重数量当成实际 native variant 数。

## 构造独立 overlay 镜像

下面仅向专用 context 复制明确列出的文件。不要把整个 checkout、模型目录或认证文件作为 Docker build context。镜像不会复用历史本地 overlay image ID。

```sh
mkdir local/pd-runtime-context
cp downloads/pd-runtime-candidate/cannbot_dsl-0.7.1-cp312-cp312-manylinux_2_28_aarch64.whl local/pd-runtime-context/
cp local/pd-ops-build/output/operators/mqsmla__qli__qsli/wheels/cannbot_arena_net-0.3.0-cp312-cp312-linux_aarch64.whl local/pd-runtime-context/
cp local/pd-ops-build/source/LICENSE local/pd-runtime-context/LICENSE.cannbot
cp build/pd-runtime-candidate/dependencies.json build/pd-runtime-candidate/verify_runtime.py local/pd-runtime-context/
docker build --network none --platform linux/arm64 \
  -f build/pd-runtime-candidate/Dockerfile \
  -t local/pd-runtime:rebuilt-candidate local/pd-runtime-context
docker image inspect local/pd-runtime:rebuilt-candidate --format '{{.Id}}'
```

[Dockerfile](../build/pd-runtime-candidate/Dockerfile) 只安装刚生成的输入，不在 build 阶段做模型或 NPU 验证。[输入检查器](../build/pd-runtime-candidate/verify_runtime.py) 核对 framework/许可 SHA、72 个 variant 目录及各自 3 个必需文件、3 个 AICPU 库和 6 个 Python 模块；这是结构检查，不验证 native kernel 执行。新算子 wheel 的 SHA 会记录在镜像内；锁中的旧 wheel SHA 只作历史身份参考，不要求重新构建逐位相同。

## 独立 CPU 导入与文件指纹

在另行分配的环境中，用新镜像执行 CPU 导入检查。只读挂入兼容 driver 库，不映射任何 NPU；不要使用 `--privileged`。此步骤沿用已使用的 CPU import/fingerprint 方法。

```sh
docker run --name pd-runtime-cpu-check --network none --cpus 4 --memory 8g \
  -e TORCH_DEVICE_BACKEND_AUTOLOAD=0 \
  -v /usr/local/Ascend/driver:/usr/local/Ascend/driver:ro \
  --entrypoint /bin/bash local/pd-runtime:rebuilt-candidate --noprofile --norc -c \
  'set -e; export LD_LIBRARY_PATH="/usr/local/Ascend/driver/lib64/driver:/usr/local/Ascend/driver/lib64/common:/usr/local/Ascend/driver/lib64:${LD_LIBRARY_PATH:-}"; source /usr/local/Ascend/ascend-toolkit/set_env.sh; python3 /opt/llmdeploy/pd-runtime/verify_runtime.py'
docker inspect pd-runtime-cpu-check --format '{{.State.ExitCode}}'
```

检查器输出两个 installed distributions 的版本、每文件 SHA 和汇总 SHA（排除 Python bytecode），同时核对实际 profile 数量。两个角色使用新构建产物时必须重新记录各自镜像身份并核对文件指纹；不继承旧 overlay 的 CPU/NPU 结论。

## 从独立配方副本渲染命令

先在 ignored `local/pd-runtime-recipe.json` 新建原 P/D 配方的副本，将副本中两个 `image.runtime_image_ids` 改为实核的新 image ID，并建立独立配置身份。保留原配方及其历史镜像身份。[render_pd.py](../scripts/render_pd.py) 的命令行输入为 `--host` 和 `--output`；自定义配方使用下面的 `command(recipe, **host)` 函数调用。

在 `local/pd-prefill-host.json` 新建目标配置。`node_ip` 的 `<NODE_IP>` 和 `interface` 的 `<NETWORK_INTERFACE>` 分别替换为所选目标的地址、实核网卡名；模型和 runtime 目录填写互不重叠的绝对路径。下例为 prefill：API 端口 28410、KV 起始端口 28500、RPC 起始端口 28700。渲染 decode 时另建 `local/pd-decode-host.json`，将 `role` 改为 `decode`、`port` 改为 28411、`kv_port` 改为 28600，并填写该目标的地址、网卡和独立 runtime/name。可选设备与目录依据目标证据填写。API key 由执行环境的 `VLLM_API_KEY` 提供，JSON 和生成脚本均不包含其值。

<!-- render-pd-host-json -->

```json
{
  "role": "prefill",
  "port": 28410,
  "kv_port": 28500,
  "rpc_port": 28700,
  "node_ip": "<NODE_IP>",
  "interface": "<NETWORK_INTERFACE>",
  "model_dir": "/models/model",
  "runtime_dir": "/runtime/pd-prefill-candidate",
  "name": "pd-prefill-candidate",
  "optional_devices": [],
  "optional_mounts": [],
  "api_host": "127.0.0.1"
}
```

将下面代码另存为新的 `local/render_pd_copy.py`。它读取明确传入的配方副本与 host JSON，以独占创建方式写出 LF shell 脚本；输出已存在时失败，保留原文件。API 的监听地址由 `api_host` 指定；跨机访问所需的地址、认证和端口检查在单独部署准入时核验。

<!-- render-pd-copy-python -->

```python
import json
from pathlib import Path
import shlex
import sys

import render_pd

recipe_path, host_path, output_path = map(Path, sys.argv[1:])
recipe = json.loads(recipe_path.read_text(encoding='utf-8'))
host = json.loads(host_path.read_text(encoding='utf-8'))
command = render_pd.command(recipe, **host)
with output_path.open('x', encoding='utf-8', newline='\n') as output:
    output.write('#!/usr/bin/env bash\nset -euo pipefail\n' + shlex.join(command) + '\n')
print(json.dumps({'state': 'RENDERED_ONLY_NOT_DEPLOYED', 'output': str(output_path)}))
```

在仓库根目录执行以下渲染命令；`PYTHONPATH` 指向该 checkout 的 `scripts/`，使函数来源明确：

```sh
PYTHONPATH="$PWD/scripts" python3 local/render_pd_copy.py \
  local/pd-runtime-recipe.json local/pd-prefill-host.json local/pd-prefill-candidate.sh
```

此调用仅渲染，不执行生成的 Docker 命令，也不读取凭据或连接目标。CPU 导入和渲染通过仍不证明真实 P/D、target/draft KV、DSpark、graph、完整上下文或性能验收。
