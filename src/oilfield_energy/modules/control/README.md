# 控制输入契约

ADR 0059 增加 `project_protection_target` 与不可变 `ProtectionTargetRecord`：保护要求和当前合法命令分别记录，能力不足不放宽原要求。保留协调器仅经公开 API 接入；当前正式单网及集群不调用该组件。语义与验证见 [保护续发决策](../../../../docs/architecture/decisions/0059-protection-target-renewal.md)。

ADR 0054 新增公开 `backup_command_candidates`：按完整源盒、本机爬坡、能力与停止能量构造 PCS 独立备用恢复，保留需完整认证的缩放候选。仅用于零 PCS 终端，不改变经济跟踪的非零目标；网络与任务采用归工作流。见 [PCS 备用模型](../../../../docs/modeling/Single_PCS_Backup.md) 及 `tests/unit/control/test_backup_recovery.py`。

ADR 0050 增加 `EnergyReturnPolicy`、`api.energy_return_command` 和 `api.certify_energy_return`：使用同一 PCS 零阶保持模型执行能量状态反馈，并保留反馈相关性、饱和区和浮点向外界。任务期限、网络证书与状态采用归单微电网工作流，参见 [共同任务续接](../../../../docs/modeling/Single_Task_Continuation.md)。

ADR 0046 为正式单微电网提供统一有限分辨率响应、过程状态盒、单次储能积分、备用命令接续和 `CommandFitProblem`。
时间常数相同的 P/Q 使用凸段能力证明，其他情形使用矩形外包；网络证书仍归 power_flow，采用及状态提交归工作流。
历史控制接口保留回归用途。当前条件与证明见 [统一单微电网](../../../../docs/modeling/Single_Microgrid_Execution.md)。

ADR 0044 新增普通设备状态盒传播和命令执行 API，公开 Tube 契约。
首步预算、全过程矩形能力、零阶保持储能能量和数值包络共用显式初态。
无网络/求解器依赖，条件性监督流程归 single_execution。

历史 ADR 0040 的 `authorize_cluster_transition` / `validate_transition_selection` 已按 ADR 0058 删除。以下说明仅保留历史证明边界：
control 拥有不可变的区域过渡提案、所选响应、条件区间上界及准备/提交契约；纯规则
在负荷/网损上界与无超调响应假设下分配集群容量预算，只延后被明确授权的普通减发。
不修改储能、保护或其他所有者动作，不依赖网络/优化 SDK，不读取未来量测。
模式 `cluster-transition-box-v1`；证明、数值向上舍入、保守性与版本兼容见
[Cluster_Transition.md](../../../../docs/modeling/Cluster_Transition.md)。

ADR 0033 新增 `api.select_storage_tracking` 和不可变储能候选/采用结果。
纯规则位于 `application_storage_tracking.py`，只读 AC 评价由装配桥注入；
安全且严格改善误差才采用，否则保持基准。物理强制限幅与硬保护独立优先，
候选不累计能量、不刷新量测或解除保护。允许依赖不变。模型、版本兼容和验证见
[Storage_Tracking.md](../../../../docs/modeling/Storage_Tracking.md)。

ADR 0030 新增 `api.track_reactive_power`，通过注入的权限/动态/AC 预测端口改进
PCC 无功跟踪；所有候选共用同一周期初状态和响应预算，安全或授权不满足则不采用。
规则不依赖求解 SDK，不提交命令，预测与实绩分开。见
[Reactive_Tracking.md](../../../../docs/modeling/Reactive_Tracking.md)。

ADR 0028 新增 `api.allocate_active_tracking` 及不可变 `ActiveTrackingResource` /
`ActiveTrackingAllocation`：在明确的能力、许可和单周期动态边界内分配剩余有功偏差。
纯规则位于 `application_active_tracking.py`，不调用网络求解或写设备；装配方独立
核验候选网络及执行证据。模型见 `docs/modeling/Active_Tracking.md`，测试见
`tests/unit/control/test_active_tracking.py` 和 `tests/integration/control/test_active_tracking.py`。

ADR 0022 新增 `api.assess_dynamic_tracking`：依据显式已采用目标和分钟输出评价
有限响应期限与期限后偏差；纯计算、不修改目标或安全状态。契约包括动态策略、
分段响应和总体证据，单位为 MW/Mvar/分钟。规则见
[动态模型说明](../../../../docs/modeling/Dynamic_PCC_Tracking.md)，测试为
`tests/unit/control/test_dynamic_tracking.py`。缺失历史证据不自动补算。

维护负责人：设备控制维护负责人；替代评审：潮流计算维护负责人。

本次公开 `contracts.PlantInputs` 与 `BusSeries`，表达带时区起点、固定步长和母线标识的不可变模拟外部输入。`load_p/load_q` 单位为 MW/Mvar，正值为总负荷消耗；风光 `available` 单位为 MW，表示可用上限，不是指令或已执行出力。

不变量：序列非空、等长、数值有限、母线唯一、P/Q 覆盖一致，负荷 P 与风光上限非负。具体设备容量和全网覆盖在用例接线处校验。窗口复制保持不可变，不能偷偷重采样。

不依赖其他业务模块，不进行 I/O 或现场设备指令执行。既有设备控制实现仍在旧模块中，通过装配桥接入；本次不宣称完成全部控制代码迁移。稳定接口为 `api.py/contracts.py`，外部输入序列化版本 `synthetic-plant-v1`，未知字段拒绝。

ADR 0005 增加纯消息接纳规则 `api.accept_coordination_message` 和不可变
`contracts.MessageCursor`。只接纳轮次不回退、发送 tick 严格更新且未过期的消息；
同轮次的新发送可作为心跳，重复/乱序发送不得刷新有效期。失联自治状态由编排方
单独记录并撤销对应待协调响应，不以 epoch=-1 覆盖已协调计划。
本规则不包含网络传输、现场命令重试或持久化；测试位于 `tests/unit/control`。

测试：`python -m pytest tests/unit/studies tests/integration/studies/test_unified_dataset.py tests/architecture -q --import-mode=importlib`。

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
与储能物理/爬坡投影位于 `application_dynamics.py`。投影的普通采用还须通过
ADR 0033 的 AC 和误差改进准入。固定网格只支持精确整比，
普通响应共用一份相对上周期实绩的爬坡额度；物理区间与爬坡区间无交集时明确记录
物理边界覆盖，不牺牲能量硬约束。既有瞬时硬保护保持独立。
模型见 `docs/modeling/Device_Dynamics.md`；新增证据 storage-dynamics-v1 独立于
已有 minute-execution-v2，公开契约生成的 schema 位于 contracts/control。
测试：`tests/unit/control/test_dynamics.py`、`tests/integration/control/test_device_dynamics.py`。
## 连续设备仿真反馈契约

`DeviceCheckpoint` 只暴露下一分钟开始前已知的储能电量和出力；
`DevicePlanUpdate` 接收从当前分钟开始、按设备 ID 对齐的前向 P/Q 命令。
`StopDeviceSession` 在边界停止并保留已完成轨迹。遗留设备引擎的连续会话实现
保留控制器和保护状态，滚动工作流通过装配桥接调用；不提供现场命令接口。
依据见 ADR 0020，回归入口为 `tests/integration/control/test_device_session.py`。

ADR 0036 补全旧设备引擎的候选能力证据：固定原 Q，在候选 P 下检查风机
比例能力、绝对 Q 和容量圆；能力信息缺失或越限时拒绝候选并保留原状态。
试算失败不驱动实际保护闭锁，规则版本为 `device-capability-v2`。
见 [设备能力模型](../../../../docs/modeling/Device_Capability_Checks.md)；
回归入口 `tests/integration/control/test_active_capability.py`。

ADR 0038 将普通无功响应统一到公开 `reactive_cycle_response`，周期证据通过
`assess_reactive_cycle` 生成。纯规则不裁剪周期初实际 Q，不导入网络/优化 SDK；
能力收缩与动态冲突显式保留，版本为 `reactive-dynamics-v1`。
模型及验证入口见 [Reactive_Dynamics.md](../../../../docs/modeling/Reactive_Dynamics.md)。

ADR 0047 增加 `storage_response_command` 及 `StorageResponseSettings/StoragePlanResponse`。
显式启用时，储能计划 P 表示下一执行子步的实际目标，通过有界逆指令驱动原响应模型。
参数与会话不一致时拒绝；名义可达和最终实际跟随分别记录。历史命令缺字段保持原义。
本模块不依赖 dispatch，状态和计划的转换由既有装配桥承担。
方程、条件和真实设备回放测试见
[Storage_Reachability.md](../../../../docs/modeling/Storage_Reachability.md)。

ADR 0049 增加 `BalancePolicy`、`balance_policy_command` 与 `certify_balance_policy`。
从已完成状态和当前总负荷生成仿射平衡指令，极值指令覆盖整个源盒并保留净功率抵消关系；
能量每周期仅积分一次。ADR 0052 将命令构造改为全部资源的局部联合投影，风机仍保留 P/Q 协调，共享无功采用绝对动作和预算；BalancePolicy 支持非负参与分数，多资源共同分担残差。认证与执行共用公式，源状态相关性保留在动作和端点外包中。
本模块不决定目标接纳、不调用网络或数值优化；相关回归为 `tests/unit/control/test_device_tube.py`。
新增反例及解析检查见 `tests/unit/control/test_local_allocation.py`，保证范围见 [逐资源模型](../../../../docs/modeling/Single_Local_Allocation.md)。


ADR 0055：`certify_cluster_device_transition` 和 `execute_cluster_device_command` 对 `cluster-device-v1` 使用同一离散一阶响应与连续线性 PCS 功率积分（包括换向）。其证书不与单网 ZOH 模型互换。网络和联合后续策略属于 cluster_execution 工作流，control 不导入潮流模块。验证：`tests/unit/control/test_cluster_tube.py`。
