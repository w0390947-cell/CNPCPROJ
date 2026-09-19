# measurements：可追溯资料捕获

维护负责人：现场数据接入维护负责人；替代评审：电网建模维护负责人。

公开入口是 `api.CaptureDataset` 和 `contracts` 中的严格清单、文件引用与不可变捕获结果。调用者通过注入读取和解码端口捕获一套资料；`domain.py` 只检查标识、角色与相对路径规则；`application` 组织读取、摘要验证及并发变更检测；`adapters` 实现本地文件和严格 JSON 解码。

不读取在线遥测、不求解潮流、不优化、不批准资料真实性。当前增量只接管资料包捕获；现有量测单位、计量范围、质量与时序规则仍由装配层接入的既有快照实现负责，不在此复制。

不变量：文件角色完整且唯一；路径不越出清单目录；普通读取必须匹配 SHA-256；捕获后只交付不可变字节；`seal` 是调用者显式选择，不能在校验失败后自动触发。不依赖其他业务模块或工作流。

契约版本 `field-dataset-v1`。破坏性变化必须升级版本；新增未知字段当前会被拒绝。外部 API 不承诺内部文件布局稳定。

验证：`python -m pytest tests/unit/measurements tests/architecture -q --import-mode=importlib`。完整操作见 [现场资料文件指南](../../../../docs/guides/Field_Dataset_Files.md)。
