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
| 图片 | 每次请求连同历史最多 4 张；客户端声明 `input: [text, image]` |
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

本次仓库整理只做静态检查、单元测试与离线补丁应用验证，没有在新机器重建镜像或重新部署，也不改变正在运行的服务。

参考：[vLLM Ascend 官方模型部署文档](https://docs.vllm.ai/projects/ascend/en/latest/tutorials/models/DeepSeek-V4.1-Flash.html)。官方 `latest` 页面会变化；本仓固定配方来自实际部署，不自动跟随该页面升级。

## 可选部署通知

`deploy_notify` 是独立的 Windows 本地观察程序，读取现有双后端网关的健康快照，不重启模型或网关。通知默认关闭，当前仅实现 `xiaoluban` 后端；发送与状态跟踪分开，后续新增其它后端时不需要改变故障判定。

将 [通知示例](hosts/notifications.example.json) 复制到被忽略的 `local/notifications/config.json`。只有本地配置填写实际群 ID、内部 API 地址、机器标签、凭据文件和接收程序路径；这些值和运行收据不进入 Git。示例地址使用文档专用网段。

启用时把 `backend: null` 改为对象，字段为 `type: "xiaoluban"`、`endpoint`、`receiver`、`token_file`。凭据文件是仅保存在本机的 JSON 对象，包含 `token` 字段；配置和命令行不接受内联 token。填写服务方提供的发送接口地址，不将真实配置提交到仓库。

发送凭据由通知服务提供方签发，按服务当前帮助申请。不要把 token 粘贴到聊天、终端命令、日志或 Git；直接保存到本地受限文件。

健康快照格式为 `{"epoch": <Unix秒>, "active_port": <端口>, "backends": [{"port": <端口>, "healthy": <布尔值>}, ...]}`。`nodes` 配置两个端口及显示标签。默认持续不可用 60 秒才告警，恢复稳定 10 秒后通知；支持单台故障、双台不可用升级、部分恢复和冗余恢复。IP 在发送前统一只保留最后一段。丢失或过期的快照显示未知，不能当作机器故障或恢复，监测空档也不计入持续故障时间。

群内状态查询需要另外启用 `incoming` 对象：`type: "welink-cli"`、`executable`（已安装的官方 CLI 绝对路径）、`bot_name`（显示名称）、`bot_uid`（机器人账号）。它只读取 `receiver` 指定群最近 100 条消息，每约 10 秒轮询；严格匹配“@机器人 状态”，不调用模型。新安装从最新消息开始，不回复旧消息；状态回复分别显示配置部署数与当前健康状况。读取依赖已有 WeLink PC 和 CLI 登录，不自动登录、不修改其它应用。高消息量超出轮询窗口时可能漏掉查询。

```powershell
python -m deploy_notify --config local/notifications/config.json --check
# This command really sends one clearly labelled activation test.
python -m deploy_notify --config local/notifications/config.json --send-test
# Hidden Windows task; use absolute local paths in the installed configuration.
./scripts/install_notifications.ps1 -Config <local-config-path> -RuntimeDir <new-private-runtime-dir> -Python <pythonw.exe-path>
```

安装器只接受新的运行目录和任务名，已有目标先停止并检查，避免覆盖其它工作。任务使用当前登录用户运行，登录后延迟 1 小时启动，随后每分钟检查，运行中不重复启动；首次安装显式立即启动。独立运行目录内的 `alert-state.json` 保存事件和群消息游标，读取此文件判断实际状态；缺凭据为 `WAITING_CREDENTIALS`，配置通过不等于投递成功。停用时停止并禁用该通知计划任务，保留状态文件以维持去重；不会影响推理服务。

同一故障通过持久状态去重。连接前失败和明确拒绝会退避重试；发送结果不明或发送途中退出时不自动重发，避免重复通知。HTTP 成功、服务接收与群历史中读回机器人消息分开记录；只有读回匹配消息才标记 `DELIVERED_GROUP_RECEIPT`，结果不明需人工核对。状态和未完成事件必须保留，删除状态会丢失去重依据。本机断电、断网或观察程序停止无法由自身通知，需要另设外部探测。

## 本地检查

```sh
python -m unittest discover -s tests -v
git diff --check
```
