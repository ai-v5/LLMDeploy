# 停止后的空闲恢复

可选本机 systemd timer 每 10 分钟运行 `python3 -B -m deploy_recovery --config /etc/llmdeploy-vllm-recovery.json`。目标容器已停止时，连续至少 7 次检查、跨度至少 1 小时，8 张 950DT 卡均健康、利用率为 0 且逐卡确认无 NPU 进程，才启动原容器。运行、暂停或初始化中的容器不重启；本模块没有 stop/kill 操作。

忙、未知、查询失败、重启或超过 15 分钟未成功观察都会重新计时。启动前再次检查目标和设备，并先持久化清空计时窗口；启动失败后重新等一小时。十分钟采样不能证明采样间隙没有短任务。

配置仅保存在机器上，字段为 `container_id`、`container_name`、`image_id`、`spec_sha256`、`docker`、`npu_smi`、`state_file`。前三项固定当前验收容器；`spec_sha256` 为 `sha256(json.dumps({'config': inspect['Config'], 'hostconfig': inspect['HostConfig']}, sort_keys=True).encode())`。工具使用已核实的绝对路径，状态目录须提前创建并只允许操作者写入。机器标识与实际配置不提交 Git。

仓内 service/timer 模板使用 `/opt/llmdeploy-vllm-recovery`；安装前核实 Python 路径，复制模块与单元，执行 `systemctl daemon-reload`、`systemctl enable --now llmdeploy-vllm-recovery.timer`。该配置不依赖 Windows 登录。检查 `systemctl list-timers llmdeploy-vllm-recovery.timer` 和 `journalctl -u llmdeploy-vllm-recovery.service`；日志只记录状态及空闲检查次数。

升级、回退或主动停用前，先停止 timer 和 service；完成后重新绑定已验收的新容器、清空旧计时状态再启用。容器 ID、名字、镜像或配置改变时恢复失败关闭，防止启动旧回退实例。停止自动恢复不会停止 vLLM。仍在运行但健康检查失败的实例由操作者处理。
