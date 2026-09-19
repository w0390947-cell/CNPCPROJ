# field_dataset：固定时刻资料工作流

维护负责人：现场数据接入维护负责人；替代评审：潮流计算维护负责人。

公开入口 `api.FieldDatasetWorkflow.execute()`，输入清单路径、输出目录、目标时刻、运行方式和显式演示标志。返回 `contracts.RunOutcome`，通过 `SnapshotEngine` 和 `ResultStore` 端口取得计算结果并发布。

只依赖 measurements 的公开 API/契约，不能直接导入旧潮流实现、优化器或现场数据内部实现。文件组装适配器处理边界文档，输出现有严格快照线格式；不包含电气公式。既有求解实现由 `bootstrap/adapters/legacy_snapshot.py` 实现工作流端口后注入。

`check` 校验摘要和指定运行点，`seal` 显式重算摘要并校验该运行点，`run` 校验后做固定状态 AC 潮流。前两者不产生安全结论；`violation` 保留计算结果，不重新调度。写入失败必须向调用者传播；只有最后出现的 `completion.json` 能表明结果包写入完整，仍须检查业务状态。

归档路径按文件 ID 独立子目录生成；输入和输出字节均有摘要。输入失败、不收敛、越限及正常状态分别报告。无现场资料时不能自动调用合成数据生成器。

验证：`python -m pytest tests/workflows tests/architecture -q --import-mode=importlib`。资料版本、旧入口兼容及适用范围见 [ADR-0001](../../../../docs/architecture/decisions/0001-field-dataset-files.md)。
