# 停止后的空闲恢复

## DeepInfer 共享机器让机恢复

`deploy_recovery.deepinfer` 用于 DeepInfer 三节点共享机器。目标容器使用 `restart=no`；宿主机的 `llmdeploy-deepinfer-recovery.service` 观察异常退出，只对固定 ID、镜像和配置的原容器执行 start，不停止或杀死其他任务。当前正在运行的模型无需重启即可切换此策略。

观察器使用 `Restart=always`、间隔 60 秒，使进程被 SIGTERM 等信号终止后仍能重新运行；systemd 将 SIGTERM 视为正常退出，`on-failure` 无法覆盖这一情况。主动执行 `systemctl stop` 仍会保持观察器停止，不会被该重启策略抵消。观察器重启不保留旧空闲窗口，也不会因此立即启动模型。

异常退出后每 60 秒串行采集一次 `npu-smi info`，没有并发或补发重试。慢调用未完成时不启动下一轮；命令超时为 20 秒。完整的 8 卡状态为 `OK` 或 `Alarm`、利用率为零、逐卡无进程才记为 IDLE；`Alarm` 保留为设备告警，不单独阻塞恢复，也不代表推理已验收。明确占用记为 BUSY，重新开始空闲窗口。其他设备状态、采集失败、超时、缺卡或解析失败记为 UNKNOWN，不等同于占用，也不立即清空窗口。漏采的整分钟补记 UNKNOWN。空闲候选窗口至少持续 3600 秒，且最近一小时 UNKNOWN 比例不超过 10%、最后一次采集成功确认空闲，才允许恢复。有限频率采样仍可能错过一分钟内的短任务。

启动前再次核对容器身份、Docker 控制事件和 8 卡空闲。采集全程单飞；先持久化清空窗口再启动，启动失败后重新等待一小时。容器运行时不采集 NPU 空闲；网关独立等待 8 个 worker 和预热完成后才恢复路由。

Docker `stop`/`kill` 控制事件会保持 MANUAL_HOLD。对已经退出的容器再次执行 `docker stop` 可能没有事件，无法表达可观察的停止意图；人工接管时必须先 `systemctl stop llmdeploy-deepinfer-recovery.service`（长期停用再 disable），然后操作模型容器。守护进程重启会清空空闲窗口；同一宿主机启动周期内保留已观察到的停止意图。无法确认控制历史、身份漂移或观察中断超过两分钟时，已停止目标保持停止，等待人工确认并启动模型后重新布防。

配置沿用下文的固定身份字段，但使用独立的 DeepInfer 路径：代码 `/opt/llmdeploy-deepinfer-recovery`、配置 `/etc/llmdeploy-deepinfer-recovery.json`、状态 `/var/lib/llmdeploy-deepinfer-recovery/state.json`。先核实 Python 路径并替换 service 中的解释器；安装 service 后执行 `systemctl daemon-reload` 和 `systemctl enable --now llmdeploy-deepinfer-recovery.service`。首次必须在已验收的运行容器上启用，不自动认领已有停止容器。旧 vLLM 恢复定时器保持关闭。

## 旧 vLLM 配方

可选本机 systemd timer 每 10 分钟运行 `python3 -B -m deploy_recovery --config /etc/llmdeploy-vllm-recovery.json`。目标容器已停止时，连续至少 7 次检查、跨度至少 1 小时，8 张 950DT 卡状态均为 `OK` 或 `Alarm`、利用率为 0 且逐卡确认无 NPU 进程，才启动原容器。`Alarm` 本身不阻塞恢复；其他设备状态仍按未知处理。运行、暂停或初始化中的容器不重启；本模块没有 stop/kill 操作。

忙、未知、查询失败、重启或超过 15 分钟未成功观察都会重新计时。启动前再次检查目标和设备，并先持久化清空计时窗口；启动失败后重新等一小时。十分钟采样不能证明采样间隙没有短任务。

配置仅保存在机器上，字段为 `container_id`、`container_name`、`image_id`、`spec_sha256`、`docker`、`npu_smi`、`state_file`。前三项固定当前验收容器；`spec_sha256` 为 `sha256(json.dumps({'config': inspect['Config'], 'hostconfig': inspect['HostConfig']}, sort_keys=True).encode())`。工具使用已核实的绝对路径，状态目录须提前创建并只允许操作者写入。机器标识与实际配置不提交 Git。

仓内 service/timer 模板使用 `/opt/llmdeploy-vllm-recovery`；安装前核实 Python 路径，复制模块与单元，执行 `systemctl daemon-reload`、`systemctl enable --now llmdeploy-vllm-recovery.timer`。该配置不依赖 Windows 登录。检查 `systemctl list-timers llmdeploy-vllm-recovery.timer` 和 `journalctl -u llmdeploy-vllm-recovery.service`；日志只记录状态及空闲检查次数。

升级、回退或主动停用前，先停止 timer 和 service；完成后重新绑定已验收的新容器、清空旧计时状态再启用。容器 ID、名字、镜像或配置改变时恢复失败关闭，防止启动旧回退实例。停止自动恢复不会停止 vLLM。仍在运行但健康检查失败的实例由操作者处理。
