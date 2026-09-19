# 山城完整模拟工作流

维护负责人：仿真研究维护负责人；替代评审：潮流计算维护负责人。

`api.ShanchengSimulation.execute(manifest, output)` 捕获一次资料，经工作流拥有的 `StudyEngine` 端口执行数值研究，再通过 `ResultStore` 发布证据。只访问登记的 measurements 公开 API，以及 field_dataset 工作流的公开归档 API/契约。

数值实现由装配根注入。业务通过需要全部具名检查通过；故障按预期阻断也可以是正确结果。`passed` 不代表所有故障场景网络都安全，不表示现场验收。异常保留已完成阶段的证据，不自动换算法或数据。

输入按摘要固定；输出使用新目录并最后发布完成标记。当前同步研究在私有临时目录计算，成功或预期失败后统一归档；进程被强制终止不会生成完整结果包。长作业调度/取消能力沿用项目后续运行设施计划。

测试：`python -m pytest tests/workflows/test_shancheng_simulation.py tests/architecture -q --import-mode=importlib`；完整 24 小时运行见 [操作指南](../../../../docs/guides/Shancheng_Complete_Simulation.md)。
