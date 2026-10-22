# 部署方案索引

新方案使用 `recipes/<代际>/<具体型号>/<引擎>/<模型>/`；指南使用 `docs/deployments/<代际>/<具体型号>/<引擎>.md`。同一代际内不同型号也独立验证，不用代际名称代替设备身份。[机器可读索引](../../recipes/catalog.json) 是导航清单，不驱动服务或替代配方。

| 硬件 | 引擎 | 指南 |
| --- | --- | --- |
| A5 / 950DT | vLLM Ascend | [指南](a5/950dt/vllm.md)，权威配方保留原路径 |
| A5 / 950DT | DeepInfer | [指南](a5/950dt/deepinfer.md)、[共享池](a5/950dt/deepinfer-pool.md) |
| A2 / 910B1 | DeepInfer 候选 | [指南](a2/910b/deepinfer.md)：原生/API 短请求通过，公开启动器与完整容量仍待验收 |
| A3 | 尚未验证 | 按 [模板](TEMPLATE.md) 新增实际型号，无占位启动命令 |

每篇包含适用范围、锁定、步骤、验证、服务/回退和公开范围。只分享使用经验，敏感信息不提供，未实测项保留未验证状态。免责声明见[根 README](../../README.md)。

A2 / 910B1 的 DeepInfer 移植使用公开的 [Eco-Tech W8A8 权重](https://www.modelscope.cn/models/Eco-Tech/DeepSeek-V4.1-Flash-1017-w8a8)，上游 revision 固定为 `12b85455a58c7df345c1abc43934624a76bac576`。[文件清单](../../recipes/a2/910b/deepinfer/deepseek-v4.1-flash-1017/model-files.json)保存该 revision 的 152 个文件大小与 SHA256，内容已逐文件匹配。原生八卡短生成、MTP 草稿接受和 API 前缀缓存已有真机证据；公开启动器目前只做过身份检查，完整 262144-token 容量仍在验证中。版本、补丁与范围见 [910B1 指南](a2/910b/deepinfer.md)。

校验这批权重时显式选择清单；完整哈希校验会读取全部权重，应在适当的空闲窗口执行：

```sh
python3 scripts/verify_model.py --root /srv/models/DeepSeek-V4.1-Flash-1017-w8a8 --manifest recipes/a2/910b/deepinfer/deepseek-v4.1-flash-1017/model-files.json --full
```
