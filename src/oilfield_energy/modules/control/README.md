# 控制输入契约

维护负责人：设备控制维护负责人；替代评审：潮流计算维护负责人。

本次公开 `contracts.PlantInputs` 与 `BusSeries`，表达带时区起点、固定步长和母线标识的不可变模拟外部输入。`load_p/load_q` 单位为 MW/Mvar，正值为总负荷消耗；风光 `available` 单位为 MW，表示可用上限，不是指令或已执行出力。

不变量：序列非空、等长、数值有限、母线唯一、P/Q 覆盖一致，负荷 P 与风光上限非负。具体设备容量和全网覆盖在用例接线处校验。窗口复制保持不可变，不能偷偷重采样。

不依赖其他业务模块，不进行 I/O 或现场设备指令执行。既有设备控制实现仍在旧模块中，通过装配桥接入；本次不宣称完成全部控制代码迁移。稳定接口为 `api.py/contracts.py`，外部输入序列化版本 `synthetic-plant-v1`，未知字段拒绝。

ADR 0005 增加纯消息接纳规则 `api.accept_coordination_message` 和不可变
`contracts.MessageCursor`。只接纳轮次不回退、发送 tick 严格更新且未过期的消息；
同轮次的新发送可作为心跳，重复/乱序发送不得刷新有效期。失联自治状态由编排方
单独记录并撤销对应待协调响应，不以 epoch=-1 覆盖已协调计划。
本规则不包含网络传输、现场命令重试或持久化；测试位于 `tests/unit/control`。

测试：`python -m pytest tests/unit/studies tests/workflows/test_shancheng_simulation.py tests/architecture -q --import-mode=importlib`。

ADR 0006 增加 `limit_synthetic_generation`、`disaggregate_executed_generation`、
`CurtailmentLedger` 与 `RestorationMonitor`。纯规则分别位于
application_execution/application_restoration；不依赖 legacy、SDK 或 I/O。
只有合成响应可在执行边界限幅，已有执行总量分解必须守恒或失败。
所有权限额独立于实际响应；恢复反馈以命令前后实际功率形成，驻留期间的外因或
质量变化锁存为 unknown。已有状态机继续负责响应容差和恢复许可。

`contracts` 新增不可变恢复命令/观测/证据与分钟导出结构；公开文件语义版本为
minute-execution-v2/restoration-evidence-v1，旧对象缺少证据不自动升级。
模型与兼容说明见 `docs/modeling/Execution_Evidence.md`；生成模式见
`contracts/control/minute_execution.schema.json`。
新增验证：`tests/unit/control/test_execution_evidence.py`、
`tests/integration/control/test_minute_execution.py`。

ADR 0007 增加 `allocate_wind_storage_target`、`wind_storage_target_bounds` 与不可变
风机实测/可用量、储能授权能力和按 ID 标识的目标契约。纯有功分配用例位于
`application_active_dispatch.py`，以当前可用量计算可达区间，保留不可控设备实测，
按“先储能吸收、后风电削减；先恢复风电、后授权放电”分配，不读取环境或执行写点。
旧山城控制器经公开接口接入，保留质量、命令与 P/Q 状态编排；不反向依赖旧模块。
模型标识 `wind-storage-dispatch-v2`，见 `docs/modeling/Shancheng_Device_Limits.md`。
原公共输入和文件模式兼容；不可达目标由错误接受改为拒绝/降级，本地越界输出改为
闭锁并等待显式恢复。验证：`tests/unit/control/test_active_dispatch.py` 与
`tests/integration/control/test_shancheng_limits.py`。

ADR 0008 增加 `execution_substeps` 和 `constrain_storage_power`，纯时间网格校验
与普通储能最终功率裁决位于 `application_dynamics.py`。固定网格只支持精确整比，
普通响应共用一份相对上周期实绩的爬坡额度；物理区间与爬坡区间无交集时明确记录
物理边界覆盖，不牺牲能量硬约束。既有瞬时硬保护保持独立。
模型见 `docs/modeling/Device_Dynamics.md`；新增证据 storage-dynamics-v1 独立于
已有 minute-execution-v2，公开契约生成的 schema 位于 contracts/control。
测试：`tests/unit/control/test_dynamics.py`、`tests/integration/control/test_device_dynamics.py`。
