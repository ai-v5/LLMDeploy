# LLMDeploy

分享已使用成功的大模型部署配方、版本锁定、构建方法与换机步骤。当前内容来自 **A5 / Ascend 950DT / 单机 8 卡** 的 vLLM Ascend 和 DeepInfer 使用经验。

**[在线文档](https://ai-v5.github.io/LLMDeploy/)** · [部署目录](docs/deployments/README.md) · [网站发布与搜索发现](docs/pages.md)

> **免责声明与公开范围**：本仓是使用经验分享，不构成兼容性、稳定性、性能、生产可用性或特定用途的保证。使用者需自行核实资源归属、软硬件条件、许可及实际效果，审阅命令后再部署。**敏感信息不提供**：不提供有效凭据、实际机器地址与账号、内部拓扑、用户会话、私有配置、原始日志或性能数据；也不分发模型权重、容器镜像和二进制依赖。示例需由使用者自行补全。新硬件、新版本和重新构建的工件必须重新验证。

原创内容采用 **AGPL-3.0-only**，见 [LICENSE](LICENSE)；上游代码和补丁保留原许可与版权，见 [第三方声明](THIRD_PARTY_NOTICES.md)。

## 选择部署方案

| 代际 / 型号 | 引擎 | 当前范围 | 入口 |
| --- | --- | --- | --- |
| A5 / 950DT | vLLM Ascend | 已使用的单机 8 卡配方，1M 总窗口 / 32K 输出 | [部署指南](docs/deployments/a5/950dt/vllm.md) |
| A5 / 950DT | DeepInfer | 已使用的单机 8 卡 API profile；可组成多后端共享池 | [部署指南](docs/deployments/a5/950dt/deepinfer.md) |
| A2 / 待确定型号 | 待验证 | 仅预留文档结构，尚无配方或支持结论 | [新增方案模板](docs/deployments/TEMPLATE.md) |
| A3 / 待确定型号 | 待验证 | 仅预留文档结构，尚无配方或支持结论 | [新增方案模板](docs/deployments/TEMPLATE.md) |

精确模型 revision、镜像及参数以各配方为准。1M 为输入与输出合计上限；足额 32K 输出时，输入最多 1,015,808 tokens。配置上限不等于该长度已经实测。实验中的双机 P/D 不属于上表已使用方案。

## 目录与导航

| 目录 | 职责 |
| --- | --- |
| [recipes/catalog.json](recipes/catalog.json) | 按代际、型号和引擎索引配方，区分已使用与计划项 |
| `recipes/<代际>/<型号>/<引擎>/<模型>/` | 新配方的参数、版本及依赖锁定 |
| [recipes/deepseek-v4.1-flash-1017/](recipes/deepseek-v4.1-flash-1017/) | 既有 vLLM 权威配方与共用模型清单；保留路径以兼容现有工具 |
| [hosts/](hosts/) / [images/](images/) | 不含认证的机器配置示例、镜像构建输入 |
| [docs/deployments/](docs/deployments/README.md) | 各硬件与引擎指南、新增方案模板 |
| `deploy_deepinfer/` / `scripts/` | 命令生成、工件校验及显式运行入口 |
| `deploy_gateway/` / `deploy_notify/` / `deploy_recovery/` | 共用的网关、通知与恢复模块 |
| `client-patches/` / `patches/` | 客户端保护及固定上游版本补丁 |
| `records/` / `tests/` | 不含敏感信息和性能数据的来源记录、本地回归 |
| `local/` / `.codex/worktrees/` | 被忽略的私有配置、临时工件与独立开发目录 |

A2、A3 后续沿同一格式增加实际型号与引擎，不继承 A5 的设备清单、拓扑、内存预算或验收结论。

## 共用服务与运维

- [多后端网关](docs/gateway.md)：认证、会话保持、共享准入、取消连接与滚动排空。
- [DeepInfer 服务池](docs/deployments/a5/950dt/deepinfer-pool.md)：多个独立 8 卡后端、SSH、Caddy 和双入口共享路由。
- [通知](docs/notifications.md) / [用量统计](docs/usage.md) / [空闲恢复](docs/recovery.md)。
- [vLLM 镜像与编译](docs/image-build.md) / [换机与客户端](docs/migration.md) / [历史部署记录](docs/deployment-record.md)。

文档和测试不会执行部署。生成器只生成新文件，不停止或替换容器；构建、启动、切换入口和安装服务均需操作者明确执行。

## 本地检查

```sh
python -m unittest discover -s tests -v
git diff --check
```

单测、协议 fixture、历史真机使用与当前硬件验收分别说明，不相互替代。
