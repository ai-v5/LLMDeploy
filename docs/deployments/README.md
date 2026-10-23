# 部署方案索引

新方案使用 `recipes/<代际>/<具体型号>/<引擎>/<模型>/`；指南使用 `docs/deployments/<代际>/<具体型号>/<引擎>.md`。同一代际内不同型号也独立验证，不用代际名称代替设备身份。[机器可读索引](../../recipes/catalog.json) 是导航清单，不驱动服务或替代配方。

| 硬件 | 引擎 | 指南 |
| --- | --- | --- |
| A5 / 950DT | vLLM Ascend | [指南](a5/950dt/vllm.md)，权威配方保留原路径 |
| A5 / 950DT | DeepInfer | [指南](a5/950dt/deepinfer.md)、[共享池](a5/950dt/deepinfer-pool.md) |
| A2 / 910B1 | DeepInfer EP16候选 | [指南](a2/910b/deepinfer.md)：双机专家常驻、MTP与缓存；原八卡兼容记录保留，262144仅配置未验收 |
| A3 | 尚未验证 | 按 [模板](TEMPLATE.md) 新增实际型号，无占位启动命令 |

每篇包含适用范围、锁定、步骤、验证、服务/回退和公开范围。只分享使用经验，敏感信息不提供，未实测项保留未验证状态。免责声明见[根 README](../../README.md)。

A2 / 910B1 的 DeepInfer 移植使用公开的 [Eco-Tech W8A8权重](https://www.modelscope.cn/models/Eco-Tech/DeepSeek-V4.1-Flash-1017-w8a8)，revision固定为`12b85455a58c7df345c1abc43934624a76bac576`。[文件清单](../../recipes/a2/910b/deepinfer/deepseek-v4.1-flash-1017-ep16/model-files.json)保存该revision的152个文件大小与SHA256，内容已逐文件匹配。当前双机固定模型源码已有文本生成、原生MTP接受、前缀缓存及32K功能证据；新的公开入口已实际启动16个worker并通过短请求、MTP及缓存检查。262144仅为配置值，未完成该容量验收；后续prefill测试不超过32768，较早候选间的质量晋级待核项仍保留。版本、补丁链、入口自身验证边界和历史八卡记录见[910B1指南](a2/910b/deepinfer.md)。

校验这批权重时显式选择清单；完整哈希校验会读取全部权重，应在适当的空闲窗口执行：

```sh
python3 scripts/verify_model.py --root /srv/models/DeepSeek-V4.1-Flash-1017-w8a8 --manifest recipes/a2/910b/deepinfer/deepseek-v4.1-flash-1017/model-files.json --full
```
