# 910B DeepInfer W4A8 源码补丁

这里提供 A2 / 910B 的 DeepInfer W4A8 Python 补丁、三个原生桥接源码和源码校验工具，供审阅和重放。**它不是完整部署配方**：本目录没有提供主服务入口、完整运行依赖、模型权重、六份 CCE 内核源码或九个原生 indexer 工件，也没有验证从这些公开文件重新构建出的模型。

补丁针对 [DeepInfer Ascend fork](https://github.com/ai-v5/deepinfer_ascend) 的 `07e5002c08bce137ab2a86a5a1fc8bb652729ad6`。对应模型是公开的 [DeepSeek-V4.1-Flash-1017 W4A8 Ascend](https://www.modelscope.cn/models/chiro2001/DeepSeek-V4.1-Flash-1017-w4a8-Ascend)，revision 为 `18291cb3607cef10558c202d6ad8edd62288bab0`。模型权重不包含在仓库中。

## 重放 Python 补丁

在独立源码目录中按顺序应用三个补丁。前两个是 W4A8 补丁所依赖的 910B 基线，并不要求部署 W8A8 服务。以下命令从本仓库根目录运行，`DEEPINFER_SRC` 指向干净的上游源码目录：

```sh
set -e
git -C "$DEEPINFER_SRC" checkout 07e5002c08bce137ab2a86a5a1fc8bb652729ad6
for patch in \
  patches/deepinfer/a2-910b-modelslim-w8a8.patch \
  patches/deepinfer/a2-910b-ep16-resident.patch \
  patches/deepinfer/a2-910b-w4a8-source.patch; do
  git -C "$DEEPINFER_SRC" apply --check "$PWD/$patch"
  git -C "$DEEPINFER_SRC" apply "$PWD/$patch"
done
python3 scripts/verify_deepinfer_910b_w4a8_source.py --source "$DEEPINFER_SRC"
```

校验器检查补丁、基线清单及 338 个 Python 文件的 LF 归一化 SHA256；它不加载模型，也不检查原生二进制。重放前请自行确认上游目录没有需要保留的改动。

## 原生工件与验证边界

当前 Python 运行时代码固定检查九个历史原生 indexer 工件的 SHA256。仓库不提供这些工件，也不提供生成它们所需的六份 CCE 源码；这部分源码的再分发来源仍需确认。三个 C++ 桥接源码位于 `recipes/a2/910b/deepinfer/w4a8-source/native-bridges/`。文件顶部的历史注释描述的是生成时的阶段，不能替代当前环境的准入记录。

如果已经合法取得全部九份**完全匹配清单哈希**的原生源码，可以将它们放入一个独立目录，用下面的工具先检查输入。`NATIVE_SOURCE` 和 `NATIVE_OUTPUT` 均由使用者自行指定；输出目录必须不存在，且不能位于源码目录内。检查输入不会编译或使用设备：

```sh
python3 scripts/build_deepinfer_910b_native.py --source-dir "$NATIVE_SOURCE" --check-inputs
```

构建步骤还需要与 `native-build-inputs.json` 一致的 CANN、Torch、torch-npu 和 GCC 工具链。`CXX` 必须指向清单中 SHA256 对应的 GCC 13 编译器；CANN 和驱动用户态库需按其安装说明配置。源码路径、编译器及依赖也可能影响最终文件身份。工具会保留失败后的输出目录，不会自动覆盖或安装工件：

```sh
export CXX="$GCC13_ROOT/bin/aarch64-conda-linux-gnu-c++"
python3 scripts/build_deepinfer_910b_native.py --source-dir "$NATIVE_SOURCE" --output "$NATIVE_OUTPUT"
```

构建器将逐项比较输出 SHA256；有差异时返回失败并保留结果。匹配哈希只证明文件身份，仍须确认已验证记录适用于当前运行环境、形状和消费者。新的文件身份必须先完成对应设备和模型验证。不能仅删除身份校验或把源码校验结果当作模型验收。此助手已在固定环境、使用另行准备的九份源码完成一次 CPU 编译，九个产物均与清单哈希一致；这次编译没有执行设备或模型验证，也不能证明其他环境均可重现。

本包没有验证 256K 上下文、长输出或性能；实际测试时应按所用环境的已授权输入上限执行。MTP、前缀缓存、专家驻留和图模式都需要在完整运行时上单独验证。
