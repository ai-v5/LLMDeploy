# A5 / 950DT / vLLM Ascend

此入口整理既有单机单 OS、8 卡 950DT 方案，不移动原配方路径，不改变引擎或线上配置。使用经验不构成适用性或性能保证，敏感信息不提供，见[免责声明](../../../../README.md)。

| 内容 | 权威入口 |
| --- | --- |
| 模型、软件、并行及容量 | [recipe.json](../../../../recipes/deepseek-v4.1-flash-1017/recipe.json) |
| 权重文件与 SHA | [model-files.json](../../../../recipes/deepseek-v4.1-flash-1017/model-files.json) |
| 私有机器配置起点 | [950dt8.example.json](../../../../hosts/950dt8.example.json) |
| 镜像与构建 | [image-build.md](../../../image-build.md) |
| 换机、生成命令、验收及回退 | [migration.md](../../../migration.md) |
| 历史范围 | [部署记录](../../../deployment-record.md)、[机器可读记录](../../../../records/950dt8-1017.json) |

```sh
python3 scripts/verify_model.py --root /srv/models/DeepSeek-V4.1-Flash-1017 --full
python3 scripts/render.py --host local/host.json --output local/run.sh
bash -n local/run.sh
```

先审阅生成文件，确认归属及八卡空闲，再显式执行。生成成功不是部署完成；精确限制以配方为准。共享网关、通知与恢复见[服务导航](../../../../README.md#共用服务与运维)。
