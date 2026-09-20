# 装配根

`adapters/project_dataset.py` 通过包资源读取统一三区域台账，经 `UnifiedDataset` 校验后投影为数值输入。设备包、Web/CLI 与山城专项共用该来源；`materialize_default_bundle()` 为默认 Web 保存内容寻址的捕获副本，不依赖 examples 或当前工作目录。见 [统一数据决策](../../../docs3/architecture/decisions/0012-unified-synthetic-dataset.md)。

新增 `shancheng_simulation.create_simulation()` 装配完整模拟研究端口，`generate_simulation()` 装配文件生成与发布。模拟用例的存量数值接线及其导入范围见 [ADR-0002](../../../docs/architecture/decisions/0002-shancheng-simulation.md)。

维护负责人：项目技术负责人；替代评审：现场数据接入维护负责人。

`create_field_dataset_workflow()` 显式装配本地读取、严格解码、固定状态求解和结果存储。模块导入时不读取现场资料或运行求解；调用工厂时采集软件版本、实现摘要和启动时刻，作为运行证据注入工作流。

适配桥 `adapters/legacy_snapshot.py` 只在私有临时目录落地已经捕获的字节，使用既有公开快照函数，不能重新打开原始资料路径。其跨存量代码的导入边在 [模块登记表](../../../docs/architecture/modules.toml) 精确列出，业务模块和工作流没有这些权限。

旧实现接入属于装配职责；物理模型、数据真实性与安全判定不得在此重写。迁移旧实现时替换端口实现，保持固定状态数值回归和失败语义。

测试：`python -m pytest tests/workflows tests/architecture -q --import-mode=importlib`。
