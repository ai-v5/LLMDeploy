# 部署方案索引

新方案使用 `recipes/<代际>/<具体型号>/<引擎>/<模型>/`；指南使用 `docs/deployments/<代际>/<具体型号>/<引擎>.md`。同一代际内不同型号也独立验证，不用代际名称代替设备身份。[机器可读索引](../../recipes/catalog.json) 是导航清单，不驱动服务或替代配方。

| 硬件 | 引擎 | 指南 |
| --- | --- | --- |
| A5 / 950DT | vLLM Ascend | [指南](a5/950dt/vllm.md)，权威配方保留原路径 |
| A5 / 950DT | DeepInfer · 单机 8 卡 P/D 混部 | [原生部署](a5/950dt/deepinfer-topologies.md)、[历史 API](a5/950dt/deepinfer.md)、[共享副本池](a5/950dt/deepinfer-pool.md) |
| A5 / 950DT | DeepInfer · 双机 8P＋8D 分离 | [P/D 分离部署](a5/950dt/deepinfer-topologies.md#单机与-pd-的公开-api-入口)；有限原生推理，双机 API 待验收 |
| A5 / 950DT | DeepInfer · 双机 16 卡 EP16 混部 | [EP16 混部部署](a5/950dt/deepinfer-topologies.md#ep16需要怎样的启动器)；分布式在线 API 启动器待交付 |
| A2 | 尚未验证 | 按 [模板](TEMPLATE.md) 新增实际型号，无占位启动命令 |
| A3 | 尚未验证 | 按 [模板](TEMPLATE.md) 新增实际型号，无占位启动命令 |

每篇包含适用范围、锁定、步骤、验证、服务/回退和公开范围。只分享使用经验，敏感信息不提供，未实测项保留未验证状态。免责声明见[根 README](../../README.md)。

A2 / 910B 的 DeepInfer 移植使用公开的 [Eco-Tech W8A8 权重](https://www.modelscope.cn/models/Eco-Tech/DeepSeek-V4.1-Flash-1017-w8a8)，上游 revision 固定为 `12b85455a58c7df345c1abc43934624a76bac576`。[文件清单](../../recipes/a2/910b/deepinfer/deepseek-v4.1-flash-1017/model-files.json)保存该 revision 的 152 个文件大小与 SHA256。量化配置为 ModelSlim W8A8_DYNAMIC，包含三层 MTP，导出配置启用了 DSpark；这些结构信息不代表完整模型或推测生成已经验收。A2 部署配方仍待端到端验证。

校验这批权重时显式选择清单；完整哈希校验会读取全部权重，应在适当的空闲窗口执行：

```sh
python3 scripts/verify_model.py --root /srv/models/DeepSeek-V4.1-Flash-1017-w8a8 --manifest recipes/a2/910b/deepinfer/deepseek-v4.1-flash-1017/model-files.json --full
```
