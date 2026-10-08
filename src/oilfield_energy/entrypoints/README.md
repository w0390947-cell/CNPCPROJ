# 命令入口

`study_inputs.main()` 对应 `oilfield-study-inputs --output ...`，只解析配方/输出路径并调用装配根。退出码 0 表示数据已生成，2 表示输入输出失败；不运行算法或判定安全。历史执行入口已归档，见 [ADR 0048](../../../docs/architecture/decisions/0048-single-retirement.md)。

维护负责人：项目技术负责人；替代评审：现场数据接入维护负责人。

`field_dataset.main()` 对应安装命令 `oilfield-field`。负责参数解析、必需时区检查、调用装配根和退出码，不解析设备数据、不判断电网安全。允许依赖装配根与工作流入口契约。

公开参数与退出码属于兼容接口。未知参数或输入失败返回 2；计算完成包含越限时返回 0，调用者必须查看 `dataset_run.json` 的状态，不能把进程成功当成网络安全。

测试覆盖安装包从仓库外目录运行、不依赖 `PYTHONPATH`。指南见 [现场资料文件指南](../../../docs/guides/Field_Dataset_Files.md)。
