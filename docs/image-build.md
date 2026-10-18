# 镜像迁移与依赖编译

## 优先：迁移已有镜像

固定运行镜像 ID：`sha256:f9bb675777595e2b3783c4b1cc6e683e4e9db2c39cc562d2be93d1b5e21cccdd`。

在已有镜像的机器导出到独立文件，传到新机器后导入。文件和校验文件不进入 Git：

```sh
docker save local/dsv41flash1017:ops071-cann920-r1 -o runtime-image.tar
sha256sum runtime-image.tar > runtime-image.tar.sha256
# 将这两个文件传到目标机；传输方式由操作者配置。
sha256sum -c runtime-image.tar.sha256
docker load -i runtime-image.tar
docker image inspect local/dsv41flash1017:ops071-cann920-r1 --format '{{.Id}}'
```

导入后必须核对 image ID。最终镜像包含 DSL 框架和三组原生算子；两处 vLLM 补丁保存在本仓，启动时应用在容器可写层。宿主机 CANN 仍需只读挂载，不能仅搬镜像。

## 从固定基础镜像重新编译 DSL 算子

基础镜像引用与平台 manifest 均在 `recipe.json`。不能将 manifest digest、image config ID 与导出 tar 的 SHA256 混为一谈。

```sh
docker pull --platform linux/arm64 quay.io/ascend/vllm-ascend@sha256:6663aec974511e6a63d1c6510503537ed83cd524c6866050b6001fc89aa386c6
docker image inspect quay.io/ascend/vllm-ascend@sha256:6663aec974511e6a63d1c6510503537ed83cd524c6866050b6001fc89aa386c6 --format '{{.Id}}'
```

已用基础 image ID 是 `sha256:70c3a5e7087afbe99e4d625e429a65e47f3975b405eead2a247dac7be2625689`。请核对后再继续。

将以下输入放进被忽略的 `downloads/`：

- `cannbot_dsl-0.7.1-cp312-cp312-manylinux_2_28_aarch64.whl`，SHA256 以 [dependencies.json](../build/dependencies.json) 为准。可以在合适的 Python/aarch64 下载环境使用 `python -m pip download --no-deps --only-binary=:all: cannbot-dsl==0.7.1`，然后严格校验文件名和哈希。
- 优先使用已核验的 `cannbot-dsl-4025ade4-raw.tar`，同样核对锁定 SHA256。若没有该归档，可将 [cannbot-dsl 官方源码](https://gitcode.com/cann/cannbot-dsl) 克隆到 `downloads/source/`，确保包含提交 `4025ade4a1000053fe38bba192d70ce5fdba835b`；构建器用 `git archive` 导出该提交，不使用工作区修改。归档文件本身因封装方式不同可有不同 SHA，不能伪称等同历史 raw tar。
- 宿主机完整 CANN 9.2.0。安装包、驱动和二进制均在仓外准备；这里不升级或覆盖宿主机 CANN。

在 Linux 仓库目录执行，下列构建容器只使用 CPU，不映射 NPU。构建耗时、资源及输出大小需要操作者按目标环境确认：

```sh
mkdir -p local/build
docker run --name llmdeploy-ops-build --network none --cpus 16 --memory 32g \
  -v "$PWD:/deployment:ro" -v "$PWD/downloads:/deps:ro" \
  -v "$PWD/local/build:/build" \
  -v /usr/local/Ascend/cann-9.2.0:/usr/local/Ascend/cann-9.2.0:ro \
  -e ASCEND_TOOLKIT_HOME=/usr/local/Ascend/cann-9.2.0 \
  -e HOME=/build/home -e OMP_NUM_THREADS=1 -e CARGO_BUILD_JOBS=16 -e MAX_JOBS=16 \
  --entrypoint /bin/bash \
  sha256:70c3a5e7087afbe99e4d625e429a65e47f3975b405eead2a247dac7be2625689 \
  --noprofile --norc /deployment/build/build_ops.sh
```

新建且空的 `local/build` 目录用于一次构建；已存在的构建结果应保留，另选目录。目标为 `dav-3510`，算子为 `mqsmla qli qsli`，同时检查三个 metadata 模块导入。`local/build/result.json` 必须是 `PASS_BUILD_AND_IMPORT_ONLY` 且构建容器退出码为 0，才保存镜像：

```sh
docker inspect llmdeploy-ops-build --format '{{.State.ExitCode}}'
docker commit llmdeploy-ops-build local/dsv41flash1017:rebuilt-ops071
docker image inspect local/dsv41flash1017:rebuilt-ops071 --format '{{.Id}}'
```

重新构建的 image ID 通常不同。把核验后的新 ID 写入 `local/host.json`，记录实际版本，再验证补丁和完整服务；不能沿用旧镜像的验收结论。历史生成算子 wheel 的 SHA 只是原产物身份，不承诺逐位可复现。

本仓的构建包装脚本依据已有成功编译命令整理；本次抽取未重跑编译。`PASS_BUILD_AND_IMPORT_ONLY` 也不等于模型可推理，仍需启动后的功能检查。
