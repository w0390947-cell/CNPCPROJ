# 资源身份与能力

ADR 0023 将既有能力语义用于协调层的逐资源 P/Q 决策，kP 随采用有功变化。
能力值仍由本模块定义；CVXPY/SCIP 约束留在各数值适配器，跨后端以解析边界
测试验证。见 [协调能力模型](../../../../docs/modeling/Coordination_Resource_Capabilities.md)。

公开边界 `contracts.py`，维护负责人：设备建模维护负责人；评审：优化调度与设备控制维护负责人。

`ResourceIdentity` 是资源 ID、母线 ID 与类别的一一映射值。当前旧模型支持每个风/光母线各一个对应资源，加单个储能和 SVG；不支持的重复映射应拒绝，不按名称猜测实体。

`WindReactivePolicy`拥有有来源的比例策略：`general-333`为精确0.333，`shancheng-300`为0.30。策略选择由输入显式声明，不能从设备名称推断。`WindReactiveCapability`独立保存kP限制、绝对Q额定值及S容量，按当前P求交集；不将绝对Q除以额定P后代替原策略。零比例表示不允许调Q，缺失比例/绝对Q仅兼容旧输入。有限性、非负性及容量有效性在构造时校验。

优化器使用该契约的绝对Q界及输入比例，并保留容量多边形近似；执行层使用`limit_at(P)`与容量圆。配方包含容量来源，新默认SC采用30%、YA_B/YA_C采用33.3%，旧包不重写。见 [ADR 0015](../../../../docs/architecture/decisions/0015-wind-reactive-policy.md)。验证入口：`tests/unit/resources/test_wind_reactive_capability.py`、`tests/integration/dispatch/test_wind_reactive_limits.py`。

`SvgCapability` 适用于 P=0 的 SVG，保留声明 Q 下界、Q 上界与 S（MVA），有效无功区间为 `[max(Qmin,-S), min(Qmax,S)]`，单位 Mvar。需正 S、有限参数且允许零无功。不声称支持 SVG 有功损耗模型。

旧算例未给 S 时，由兼容模型明确沿用原 Q 包络推得 S；新完整研究配方必须传入声明 S。规则用于优化、分钟分配、控制器限幅，原始声明同时进入分钟快照供独立固定状态设备约束检查。见 ADR-0011、资源能力单元测试和滚动流程回归。
