# 可复现模拟研究输入

维护负责人：仿真研究维护负责人；替代评审：设备控制维护负责人。

事件契约及适用性规则归本模块：`contracts.ScenarioEvent` 为可执行事件类型，旧
`scenario_events` 转导出该类型；`ScenarioEventRecord` 共享字段定义，仅用于读取历史描述。
`api.validate_scenario_events` 在求解前检查模式、
区域和轮次；`compile_communication_events` 与 `communication_event_effect` 定义
规范化通信窗口、失联并集和全局丢包最高概率。它们是无 I/O 的纯规则。
执行证据区分完整、部分和未到达窗口，详见
[ADR 0010](../../../../docs/architecture/decisions/0010-study-events.md)。
回归：`tests/unit/studies/test_events.py`、`tests/integration/studies/test_event_execution.py`。

`api.generate_inputs(StudySpec)` 同时返回日前预测、日内预测和分钟外部输入。契约定义配方、设备边界、负荷和储能初值；领域层使用显式种子的 `random.Random` 和解析日曲线生成模拟值，应用层组织三类生成调用。无文件读写、无优化器、无现场数据兜底。

允许通过公开契约依赖 `control.contracts`，用于生成控制外部输入；该新增边见 [ADR-0002](../../../../docs/architecture/decisions/0002-shancheng-simulation.md)。输入参数不会被修改。相同配方和兼容 Python 实现应生成相同值；跨环境数值复算采用容差。

配方版本 `shancheng-simulation-v1`。所有数据明确模拟、无现场批准。配方中的网络载荷还必须经过既有严格网络契约校验；不能把配方模式校验等同于电气适用性验证。

静态网络研究归档另有 `NetworkScenarioSummaryMetadata`，版本
`network-scenarios-v2`，仅规定元数据，不启动潮流或计算第二套安全结论。
模式由 `tools/export_network_scenario_contract.py` 与旧计算结果类型共同生成。
新增字段、输入未知语义及旧消费者迁移见 ADR 0009。

公式、参数假设与运行说明见 [完整模拟指南](../../../../docs/guides/Shancheng_Complete_Simulation.md)。测试：`python -m pytest tests/unit/studies tests/workflows/test_shancheng_simulation.py -q --import-mode=importlib`。
