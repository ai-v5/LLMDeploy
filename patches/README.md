# 固定版本补丁

补丁只适用于 `recipe.json` 锁定的 vLLM / vLLM Ascend 源码。`manifest.json` 保存源文件原始 SHA256、修改后 SHA256 和 patch 文件 SHA256；启动时逐项核对，不对陌生版本尝试模糊套用。

| 补丁 | 原因和范围 |
| --- | --- |
| `model-runner-eager-fallback.patch` | 当调度结果为 `CUDAGraphMode.NONE` 且模型声明 `requires_uncompiled_fallback` 时，跳过 compiled 分支。FULL Graph 路径不变。 |
| `decoder-prefill-no-trimming.patch` | 暂停该固定版本可选的 decoder prefill trimming。Prefix Cache 与原生 SWA 配置保留，不能把这个辅助层优化和 Prefix Cache 本身混淆。 |

`apply_patches.py` 修改容器层中的源码，不修改宿主机 CANN，也不覆盖原始模型。所有源文件都先校验并做 `git apply --check`，然后才应用；已经是目标哈希的文件保持不变。

责任范围：本仓固定配方维护者。清理事件：升级镜像/上游提交时，先查对应问题是否修复；只有原生新版本通过相同 eager、冷请求检索、缓存、图片和工具用例后，才删除相应补丁并更新配方。不要把补丁无条件带到新版本。

本仓原创修改采用 AGPL-3.0-only。这些差异中保留的上游代码片段来自 Apache-2.0 源文件；补丁应用后保留上游源文件原有许可和版权声明。上游许可全文及归属见 [THIRD_PARTY_NOTICES.md](../THIRD_PARTY_NOTICES.md)。本仓没有复制整个上游项目。
