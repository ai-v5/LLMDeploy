# 停止后的空闲恢复

## 普通模型的空闲恢复

`deploy_recovery.ordinary_recovery` 只恢复固定 ID、名称、镜像和配置指纹的原容器，不停止或杀死模型。首次布防必须观察到该容器在运行，既有的已停止容器不会自动认领；机器重启后只有已认领的运行意图才进入新的恢复窗口。观察到 Docker 对目标的 stop、kill 或销毁时保持停止，随后人工重新启动形成新运行代际才重新布防。普通退出码为零且不属于机器重启时也保持停止；仅 Docker 守护进程重启导致的正常退出不作为机器重启处理。

非零退出和机器重启以尽力判断处理。宿主机直接发出的信号可能与程序异常具有相同退出码；事件缓冲可能丢失记录，重启前后的墙钟可能变化，因此无法保证所有人工停服都被识别。控制历史不完整时，核对目标稳定性并清空旧采样，再从头观察；发现明确的控制操作则保持停止。观察器重启会清空空闲采样，采样间隔超过 150 秒也会重计；较短的采样间隙仍可能漏掉短任务。

退出后至少两小时的观察窗需要逐次确认八卡状态、无 NPU 进程、没有其他工作容器及没有受监控的设备句柄。忙、未知、设备布局变化或采样失败都会重置窗口；启动前再次核对目标、控制事件和当前资源。重置后的计时先写入状态文件再请求启动，失败后重新等待。采样不能替代所有使用者共同遵守的资源租约：最终检查与启动之间仍可能有其他任务进入。

本模块要求 Docker 服务 active 且 `docker.socket` inactive；状态不满足时不连接、不启动模型。服务和 socket 检查与连接之间仍有竞争。若 Docker 启动依赖 socket，重启后必须先确保 daemon 的连接方式和 socket 状态符合要求，否则恢复保持等待。恢复请求成功或容器处于 running 只表示启动命令或进程状态，推理健康与网关就绪必须另行核验。

配置沿用固定身份字段 `container_id`、`container_name`、`image_id`、`spec_sha256`、`docker`、`npu_smi`、`state_file` 和可选的 `non_workload_containers`，并要求 `device_patterns` 列出机器实际需要检查的设备路径模式与数量。每项为 `{"pattern": "/dev/...", "count": 设备数}`；至少覆盖八个 `/dev/davinci[0-7]` 和 `/dev/davinci_manager`，同时包含现场实际使用的其他设备类型。可选 `non_workload_processes` 只用于操作者确认可以共存的 root 系统服务，逐项固定 `service`、`exe_path`、`exe_sha256`、`cmdline_sha256`、`environ_sha256`；仅扫描前后均核验到精确 MainPID、进程代际、控制组及各项指纹的持有者可跳过。缺项、数量不符、权限不足、身份变化或其他持有者均阻止启动。机器专属配置和实际路径不提交到本仓。

安装时须一同复制 `deploy_recovery/__init__.py`、`watchdog.py`、`deepinfer.py`、`device_holders.py` 和 `ordinary_recovery.py`，并按实际路径填写 `llmdeploy-ordinary-recovery.service`；配置、状态目录和 service 均使用任务专属名称，禁止覆盖现有部署。服务使用 `PartOf=docker.service`，不附带停止模型的钩子。启用后检查状态文件、服务进程、目标容器及推理健康；没有执行真实故障和完整两小时恢复时须明确标为未验证。

## DeepInfer 共享机器让机恢复

`deploy_recovery.deepinfer` 用于 DeepInfer 共享机器。目标容器使用 `restart=no`；宿主机的 `llmdeploy-deepinfer-recovery.service` 观察容器退出和宿主机重启后的退出状态，只对固定 ID、镜像和配置的原容器执行 start，不停止或杀死其他任务。当前正在运行的模型无需重启即可切换此策略。

观察器使用 `Restart=always`、间隔 60 秒，使进程被 SIGTERM 等信号终止后仍能重新运行；systemd 将 SIGTERM 视为正常退出，`on-failure` 无法覆盖这一情况。单元随 Docker 的显式 stop/restart 停止或重启，并在 Docker 启动时由启用的依赖链接拉起；观察器自身不依赖启动 Docker。每次使用 Docker CLI 前检查 Docker 服务处于 active，且 `docker.socket` 未启用运行；若服务不可用、socket 激活或状态无法确认，则不连接、不启动模型。检查和连接之间仍有竞争，不能把它当作跨进程原子锁。同一开机周期内重启观察器时，停止目标保留下一次检查的原定时间；空闲记录只有在下次检查确认 Docker 控制历史完整后才能继续使用，重启本身不会立即启动模型。

目标运行时每 60 秒检查容器状态；退出后串行采集 `npu-smi info`。最近一次观察为空闲时，下次在 2 分钟后检查；发现设备或其他工作容器占用、或观察结果未知时，下次在 20 分钟后检查。采集不并发，也不补发重试；慢调用未完成时不启动下一轮，命令超时为 20 秒。完整的 8 卡状态为 `OK` 或 `Alarm`、利用率为零、逐卡无进程，同时没有其他工作容器运行，才记为 IDLE。`Alarm` 保留为设备告警，不单独阻塞恢复，也不代表推理已验收。明确占用、采集失败、超时、缺卡、解析失败或漏采都会重新开始空闲窗口。窗口至少持续 7200 秒并包含至少 60 次成功的空闲采样，才允许恢复。

启动前再次核对容器身份、Docker 控制事件、8 卡空闲和其他运行容器。采集全程单飞；先持久化清空窗口再启动，启动失败后重新等待两小时。容器运行时不采集 NPU 空闲；网关独立等待 8 个 worker 和预热完成后才恢复路由。有限频率采样可能错过短任务，最终检查与启动之间也没有跨用户的原子资源锁；机器上存在不使用 Docker 且尚未占用 NPU 的工作，或其他调度器并发启动时，不能保证绝对互不影响。需要强隔离的场景须接入各方共同遵守的资源调度或租约。

Docker `stop`/`kill` 停止目标后同样进入空闲恢复，包括退出码为零的正常退出。观察到新的目标启停事件或运行时间变化时，丢弃旧空闲记录并从头计时；启动前若发生启停竞争，取消本次启动并重新观察。需要跨 Docker 启停持续停用时，先 `systemctl disable --now llmdeploy-deepinfer-recovery.service`，然后操作模型容器。每次检查已布防的停止目标，都核实 Docker 守护进程未在间隔期间重启、可读取的全局事件少于其 256 条上限。历史不完整时清空旧窗口，重新核实固定目标和当前控制状态，再从零观察；控制不可读、目标销毁或身份漂移时保持停止。事件持续过密可能使窗口反复清空、恢复推迟；事件缓冲在查询期间仍可能发生覆盖，不能完全证明采样间没有短任务。若间隔超过上次计划周期的两倍，或空闲样本相隔达到四分钟，则重新开始空闲计时。至少 60 条保留样本的首尾必须跨越 7200 秒，有效空闲样本间隔略有延迟时实际恢复可能晚于两小时。

宿主机重启会为已认领的目标开启新的恢复窗口，包括重启后观察器首次检查前发生的 stop/kill；要求状态中的可信身份与目标 ID、名称、镜像和配置指纹全部匹配，且本次开机后的可见事件没有目标销毁。新窗口从头采样两小时，不复用重启前的记录。只有实际观察到固定目标运行，或操作者显式核验并重新布防，才能建立可信身份；缺失、损坏或不匹配的状态不自动认领已有停止容器。旧版停止状态没有完整可信身份，升级后需由操作者核验并显式重新布防，不能只凭旧版 `armed` 标志推定。

配置沿用下文的固定身份字段，但使用独立的 DeepInfer 路径：代码 `/opt/llmdeploy-deepinfer-recovery`、配置 `/etc/llmdeploy-deepinfer-recovery.json`、状态 `/var/lib/llmdeploy-deepinfer-recovery/state.json`。先核实 Python 路径并替换 service 中的解释器；安装 service 后执行 `systemctl daemon-reload` 和 `systemctl enable --now llmdeploy-deepinfer-recovery.service`。首次必须在已验收的运行容器上启用，不自动认领已有停止容器。旧 vLLM 恢复定时器保持关闭。

可选 `non_workload_containers` 仅用于经过操作者确认的系统基础容器；每项必须同时固定 `container_id` 和 `image_id`。其他运行容器、替换后的基础容器或身份无法核实均阻止启动。对已授权的旧版停止目标或需要修复的未布防状态，先停观察器并保存原状态；核实该机器与固定容器确实属于恢复池后，执行 `python3 -B -m deploy_recovery.deepinfer --config <配置路径> --rearm-stopped`，再启动观察器。该动作核对固定身份、启动周期、上次运行时间及后续 Docker 控制事件，记录可信身份并从零计时，不启动模型；身份不匹配或控制事件变化时拒绝布防。

## DeepInfer 的 SSH 登录提示

对使用 cgroupfs v1 的共享机器，可根据恢复配置中固定的 DeepInfer 容器身份安装登录提示。安装前核实配置指向目标实例，然后在宿主机以 root 执行 `python3 -m deploy_recovery.ssh_notice --config <恢复配置路径> --install`。每次查询 Docker 前会检查 Docker 服务处于 active 且 `docker.socket` 未运行，状态不满足时拒绝连接；检查与连接之间仍可能发生状态变化。安装器读取容器身份和进程状态，在 `/etc/profile.d/llmdeploy-deepinfer-notice.sh` 原子创建提示；已有同名文件时拒绝覆盖。无需修改 SSH 配置、授予 Docker 权限或重启服务。

提示仅对读取 `/etc/profile` 的交互式 SSH 登录生效；远程命令、文件传输和非 SSH 会话不输出提示。普通用户通过容器 cgroup 和进程命名空间只读检查该固定容器的存活状态，容器停止后不显示；这不等同于 HTTP 健康检查。重建容器会改变其固定身份，须重新核对并更新恢复配置和登录提示。

## 旧 vLLM 配方

可选本机 systemd timer 每 10 分钟运行 `python3 -B -m deploy_recovery --config /etc/llmdeploy-vllm-recovery.json`。目标容器已停止时，连续至少 7 次检查、跨度至少 1 小时，8 张 950DT 卡状态均为 `OK` 或 `Alarm`、利用率为 0 且逐卡确认无 NPU 进程，才启动原容器。`Alarm` 本身不阻塞恢复；其他设备状态仍按未知处理。运行、暂停或初始化中的容器不重启；本模块没有 stop/kill 操作。

忙、未知、查询失败、重启或超过 15 分钟未成功观察都会重新计时。启动前再次检查目标和设备，并先持久化清空计时窗口；启动失败后重新等一小时。十分钟采样不能证明采样间隙没有短任务。

配置仅保存在机器上，字段为 `container_id`、`container_name`、`image_id`、`spec_sha256`、`docker`、`npu_smi`、`state_file`。前三项固定当前验收容器；`spec_sha256` 为 `sha256(json.dumps({'config': inspect['Config'], 'hostconfig': inspect['HostConfig']}, sort_keys=True).encode())`。工具使用已核实的绝对路径，状态目录须提前创建并只允许操作者写入。机器标识与实际配置不提交 Git。

仓内 service/timer 模板使用 `/opt/llmdeploy-vllm-recovery`；安装前核实 Python 路径，复制模块与单元，执行 `systemctl daemon-reload`、`systemctl enable --now llmdeploy-vllm-recovery.timer`。该配置不依赖 Windows 登录。检查 `systemctl list-timers llmdeploy-vllm-recovery.timer` 和 `journalctl -u llmdeploy-vllm-recovery.service`；日志只记录状态及空闲检查次数。

升级、回退或主动停用前，先停止 timer 和 service；完成后重新绑定已验收的新容器、清空旧计时状态再启用。容器 ID、名字、镜像或配置改变时恢复失败关闭，防止启动旧回退实例。停止自动恢复不会停止 vLLM。仍在运行但健康检查失败的实例由操作者处理。
