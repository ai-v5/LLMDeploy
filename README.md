# LLMDeploy

用于保存可以迁移的模型部署配方。首个配方来自已经运行的 **DeepSeek-V4.1-Flash-1017 / 单 OS、8 卡 Ascend 950DT**。

本仓保存部署配置、版本锁定、源码补丁、依赖编译方法与换机记录。认证材料、用户会话、原始日志、性能结果、权重和二进制依赖不进入 Git。

本项目原创内容采用 **AGPL-3.0-only**，见 [LICENSE](LICENSE)。补丁中保留的上游代码片段继续遵守原许可与版权声明，见 [第三方声明](THIRD_PARTY_NOTICES.md)。

## 当前配方

| 项目 | 配置 |
| --- | --- |
| 模型 | `deepseek-ai/DeepSeek-V4.1-Flash-1017` |
| Revision | `1bae0e1088993b9b82de08f59de5be5a34e42170` |
| 并行 | TP1 / DP8 / EP8 / PP1 / CP1，单机单 OS |
| 总上下文 / 客户端输出上限 | 524288 / 32768 tokens；输入与输出合计不超过总上下文 |
| 调度 / Graph | 每 DP 最多 4 条序列；FULL_DECODE_ONLY，捕获 `[1,2,4]` |
| 图片 | 每次请求连同历史最多 640 张，仍受上下文限制；客户端声明 `input: [text, image]` |
| 缓存 | Prefix Cache、SWA bounded replay 配置开启，缓存用量明细开启 |
| 主存 | Engram CPU offload + DP 共享；CPU KV offload 关闭 |
| API | 容器使用 host 网络，仅绑定主机 `127.0.0.1:8900` |

这不是 TP8 配方。所有精确版本和参数以 [recipe.json](recipes/deepseek-v4.1-flash-1017/recipe.json) 为准。

## 换机最短流程

在目标 **Linux aarch64 / 8 卡 950DT** 上操作。先独立确认机器归属、卡健康、空闲情况和磁盘空间，不要停止不属于此次部署的容器。目标机器的驱动、CANN、RDMA 设备和拓扑配置必须重新检查。

1. 获取本仓，将 [hosts/950dt8.example.json](hosts/950dt8.example.json) 复制为被忽略的 `local/host.json`，修改权重、缓存、工件、CANN 路径、容器名和端口。物理设备仍为 `0..7`。修改镜像时填写核验后的完整 image ID。
2. 准备固定 revision 的完整权重，以及固定运行镜像。优先使用 [镜像与编译](docs/image-build.md) 中的已有镜像迁移方式。
3. 在目标机完整校验权重：

   ```sh
   python3 scripts/verify_model.py --root /srv/models/DeepSeek-V4.1-Flash-1017 --full
   ```

   不带 `--full` 只查文件大小，不能证明内容完整。

4. 检查 `npu-smi info`、`uname -m`、`docker info`、目标 CANN 的 `set_env.sh`，并确认 `/usr/local/Ascend/driver/topo`、本机 RDMA 设备和驱动挂载路径正确。不要从别的机器复制 `/etc/hixlep`、`/lib/route.conf` 或 HCCL 拓扑文件。
5. 在目标机仓库目录生成并阅读启动脚本：

   ```sh
   python3 scripts/render.py --host local/host.json --output local/run.sh
   bash -n local/run.sh
   less local/run.sh
   ```

   生成器只写文件。它自动枚举目标机已有的 `/dev/uburma/*` 和可选拓扑挂载；不要在 Windows 上生成后直接拿去启动 Linux。输出文件必须不存在，避免覆盖旧配置。

6. 确认目标卡可供本次使用后，显式执行 `bash local/run.sh`。它启动新容器，不会停止或替换旧容器；同名容器或占用端口需要操作者先查明归属。启动时会校验并应用两处补丁，版本或哈希不符就退出。
7. `docker logs --tail 80 <容器名>` 检查加载状态。就绪后执行：

   ```sh
   curl --fail http://127.0.0.1:8900/health
   python3 scripts/smoke.py --base http://127.0.0.1:8900/v1
   ```

   smoke 只验证文本、SSE、重复前缀缓存用量，不是完整模型质量验证。图片、工具调用和实际客户端还要按 [换机核对单](docs/migration.md) 验证。

## 文件入口

- [镜像迁移与依赖编译](docs/image-build.md)：固定基础镜像、DSL 算子编译、镜像导出导入。
- [换机和客户端重配](docs/migration.md)：机器差异、SSH 转发、dsh 字段、验收与回退。
- [补丁说明](patches/README.md)：两个补丁的作用、适用版本和删除条件。
- [部署过程记录](docs/deployment-record.md)：当前方案如何收敛，以及已知边界；不包含性能结果。
- [机器可读记录](records/950dt8-1017.json)：已运行部署的配置来源和功能验收范围。
- [可选部署通知](docs/notifications.md)：本地通知与群内状态查询。
- [本机 token 统计](docs/usage.md)：持久用量与群内汇总。
- [双后端会话路由](docs/gateway.md)：会话保持、均衡与故障切换。

参考：[vLLM Ascend 官方模型部署文档](https://docs.vllm.ai/projects/ascend/en/latest/tutorials/models/DeepSeek-V4.1-Flash.html)。官方 `latest` 页面会变化；本仓固定配方来自实际部署，不自动跟随该页面升级。

## 本地检查

```sh
python -m unittest discover -s tests -v
git diff --check
```
