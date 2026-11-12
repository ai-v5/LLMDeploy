# A5 / 950DT / vLLM Ascend 单机 5 卡

这是 DeepSeek V4.1 Flash-1017 的单机五卡部署方案：张量并行 TP1、数据并行 DP5、专家并行 EP5，并启用 DSpark 推测生成。五个 DP 进程共同提供一个 OpenAI 兼容模型服务。

## 版本和准备

| 项目 | 配置 |
| --- | --- |
| 硬件 | 一台 Linux aarch64 主机，5 张 Ascend 950DT |
| 模型 | `deepseek-ai/DeepSeek-V4.1-Flash-1017`，revision `1bae0e1088993b9b82de08f59de5be5a34e42170` |
| 服务模型名 | `ds-v4.1-flash-1017` |
| 运行镜像 ID | `sha256:f9bb675777595e2b3783c4b1cc6e683e4e9db2c39cc562d2be93d1b5e21cccdd` |
| 软件 | CANN 9.2.0；vLLM 源码 `db9527a46873454610df6dbedf79a36d6bf1a7f6`；vLLM Ascend 源码 `f978348e9045fb045a4281d189339ef2ca1f1a45` |
| 容器共享内存 | 256 GiB；Engram 使用 CPU offload 和 DP 共享内存 |
| 专家映射 | 额外使用[五卡补丁](../../../../patches/vllm-ep5-expert-map.patch)，其[前后源码及补丁 SHA256](../../../../patches/manifest-ep5.json)固定 |

镜像准备、基础依赖和源码版本见[镜像文档](../../../image-build.md)及[八卡版本锁定](../../../../recipes/deepseek-v4.1-flash-1017/recipe.json)。模型文件清单与八卡方案相同，先在目标机完成全量校验：

```sh
python3 scripts/verify_model.py --root /srv/models/DeepSeek-V4.1-Flash-1017 --full
```

为五卡实例准备独立容器和运行目录：挂载模型至 `/model`、本仓库至只读 `/deployment`、可写运行目录至 `/runtime`；挂载目标机的 CANN、驱动和所选五张设备，并配置 `ASCEND_RT_VISIBLE_DEVICES` 对应这五张设备。设备与目录挂载可参考[八卡生成器](../../../../scripts/render.py)；该生成器当前固定八卡，不能直接生成本方案的启动命令。

## 补丁和启动

主模型有 384 个实际路由专家，DSpark 草稿模型有 128 个。五卡专家并行时，补丁分别将映射槽位补齐至 385 和 130；路由器和模型权重中的真实专家编号不变。

在新容器的可写层中，先设置 CANN 环境并应用固定补丁。五卡选项会同时检查和应用基础补丁及五卡专家映射补丁；源文件或补丁哈希不匹配时停止。

```sh
export ASCEND_TOOLKIT_HOME=/usr/local/Ascend/cann-9.2.0
export ASCEND_HOME_PATH="$ASCEND_TOOLKIT_HOME"
export ASCEND_OPP_PATH="$ASCEND_TOOLKIT_HOME/opp"
export VLLM_USE_V2_MODEL_RUNNER=1 HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1
export VLLM_ENGINE_READY_TIMEOUT_S=36000
export CANNBOTDSL_CACHE_DIR=/runtime/cannbot-dsl-0.7.1-cann920
export LD_LIBRARY_PATH=/usr/local/python3.12.13/lib:/usr/local/lib:/usr/local/Ascend/driver/lib64:/usr/local/Ascend/driver/lib64/driver:/usr/local/Ascend/driver/lib64/common
source "$ASCEND_TOOLKIT_HOME/set_env.sh"
python3 /deployment/scripts/apply_patches.py --profile five-card
```

根据目标机的 CANN 挂载位置调整上述路径，再启动服务：

```sh
vllm serve /model --host 127.0.0.1 --port 8900 \
  --served-model-name ds-v4.1-flash-1017 \
  --tokenizer-mode deepseek_v41 --reasoning-parser deepseek_v41 \
  --tool-call-parser deepseek_v41 --enable-auto-tool-choice \
  --tensor-parallel-size 1 --data-parallel-size 5 --data-parallel-size-local 5 \
  --enable-expert-parallel --quantization deepseek_v4_fp8 \
  --max-model-len 1048576 --max-num-batched-tokens 2048 --max-num-seqs 4 \
  --block-size 128 --enable-prefix-caching --swa-bounded-replay \
  --limit-mm-per-prompt '{"image":640}' \
  --engram-config '{"cpu_offload":true,"dp_shared_memory":true}' \
  --model-loader-extra-config '{"enable_multithread_load":true,"num_threads":16}' \
  --safetensors-load-strategy lazy \
  --additional-config '{"enable_cpu_binding":true,"multistream_engram_overlap":true,"multistream_overlap_shared_expert":true,"combine_quant_mode":4}' \
  --compilation-config '{"mode":3,"cudagraph_mode":"FULL_DECODE_ONLY","cudagraph_capture_sizes":[6,12,18,24]}' \
  --enable-prompt-tokens-details \
  --speculative-config '{"method":"dspark","num_speculative_tokens":5,"enforce_eager":false}'
```

## 验证范围

该版本组合已完成短文本生成、流式响应和五个 DP 各自的基本请求验证；专家映射检查覆盖了各实际专家的归属。新环境启动后，应检查 `/health`、`/v1/models`，并分别验证普通文本、流式响应及实际客户端功能。

`1048576` 是此处的最大上下文配置值，不代表本五卡方案已完成接近该长度的验证。图片上限、长输出和并发场景也需要在目标环境单独验证。
