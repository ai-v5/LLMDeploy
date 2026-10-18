# 已有部署记录摘要

记录截至北京时间 2026-10-19。这里只保存部署事实和问题处理，不迁入原始会话、认证材料或性能结果。

1. 模型采用 Hugging Face 固定 revision 的原始 1017 权重；没有覆盖原始权重来适配启动方案。全部模型文件的路径、大小和内容身份保存在 `model-files.json`。
2. 固定 vLLM Ascend 基础镜像及其源码提交。仅看包版本字符串不足以识别这份开发镜像，必须同时核镜像 ID 和两个项目的 Git commit。
3. 宿主机 CANN 9.2.0 只读挂载。在基础镜像中安装 DSL 0.7.1，按源码提交构建三个原生算子及 metadata 模块，形成派生运行镜像；没有在宿主机覆盖 CANN 安装。
4. 单 OS 8卡采用 TP1/DP8/EP8。曾尝试 TP8，但固定镜像在该设备上拒绝此配置，因此不将 TP8 文档示例直接当作本机可用配方。
5. 使用 V2 runner、FULL_DECODE_ONLY Graph `[1,2,4]`。模型声明的 eager fallback 需要 runner 小补丁才能在相应路径跳过 compiled 分支；FULL decode replay 分支保持原逻辑。
6. 开启 Prefix Cache 和 SWA bounded replay 配置后，某个中等长度冷请求检索暴露正确性问题。禁用可选 decoder prefill trimming 后，通过相同功能用例；该补丁不删除原生 Prefix/SWA 缓存配置。完整旧失败过程仍留在原工程任务目录，不迁入本仓。
7. 保持总窗口 512K、客户端输出上限32K、每DP并发4。CPU/NUMA绑定与CPU KV offload关闭；Engram CPU共享开启。
8. 迁移到另一台同类目标机时，重新确认设备、镜像、权重、CANN和路径；实际机器身份与实例标识保留在仓外私有记录中。
9. 图片输入上限从0改为4，并给 dsh 模型声明 `[text,image]`。已有实例完成图片内容、换图、历史图片和客户端功能检查。
10. 添加 `--enable-prompt-tokens-details`，API开始上报缓存 token 明细，原生客户端和Web显示链路通过；Desktop的既有会话也观察到新字段。旧用量缺失导致整会话累计百分比被历史输入稀释，不是缓存重新关闭。

当前配方曾在参考机运行；本仓提取没有重新编译、重新启动或迁移真实服务。完整窗口与输出的功能记录属于先前部署实例，不能冒充每个新镜像或新机器都通过。

原始事实来源：已有任务中的固定 candidate、权重 manifest、派生镜像/编译记录，以及本次只读读取的运行镜像版本/commit。仓内 `records/950dt8-1017.json` 只保留部署范围的摘要。
