# 协调区域设备能力模型 v1

模型标识：`coordination-resource-capabilities-v1`。实现：
`oilfield_energy.regional_control.RegionalConvexController`（既有 CVXPY 数值适配器）。
设备语义由 `resources.contracts` 拥有；决策见 ADR 0023。

ADR 0024 将随滚动窗口变化的输入改为 CVXPY Parameter，并复用结构兼容的
编译模型；上述模型标识与数学方程保持不变。每个窗口仍重新绑定预测、损耗、
电价、储能初末电量和安全边界，并独立判断收敛、设备及 AC 可行性。
复用与全新模型的数值对照见 `tests/integration/dispatch/test_coordination_workspace.py`。

## 变量、方程与单位

P 为 MW，Q 为 Mvar，S 为 MVA，E 为 MWh。时间轴沿用算例区间。
风/光 P 非负；资源 Q 正值为向区域注入无功；PCC P/Q 正值为从上级受入。
储能充电 C、放电 D 均非负，向区域的净注入为 D−C。

每个风机 i、时段 t 的约束为：

- `0 ≤ P[i,t] ≤ min(预测可用有功[i,t], 额定有功[i])`；
- `−Qabs[i] ≤ Q[i,t] ≤ Qabs[i]`，由现有能力契约给出有效绝对边界；
- 声明 k 时，`−k[i] P[i,t] ≤ Q[i,t] ≤ k[i] P[i,t]`；k=0 明确禁止调 Q；
- 容量内接多边形：对 `j=0,…,N−1`，
  `cos(2πj/N)P + sin(2πj/N)Q ≤ S cos(π/N)`。

该多边形与区域 MISOCP 使用相同 N 和方向，比分钟执行的容量圆保守。
P 是本次优化采用的出力，限发后无功界同步变化。未声明 k/绝对 Q 的旧输入
沿用已有容量语义，不补造新的策略。

光伏使用自身 P 上限及同一容量多边形；未授权时 Q=0。
储能未参与时 C=D=Q=0；参与但未授权调 Q 时 Q=0。
参与时约束 `C+D ≤ Pmax`，它是执行模型充/放互斥门控的凸包，
并对 `(D−C,Q)` 施加容量多边形。能量递推仍为
`E[t+1]=E[t]+ηc C[t] Δt−D[t] Δt/ηd`，保留初始、终端和上下界。
连续松弛仍不能独立证明充放互斥，采用前由执行层认证。

SVG 按 `SvgCapability` 使用
`max(Qmin,−S) ≤ Qsvg ≤ min(Qmax,S)`。P=0；允许正负范围不同。

聚合变量为 `Prenew=ΣPwind+ΣPpv`、
`Qsupport=ΣQwind+ΣQpv+Qstorage+Qsvg`；原 P/Q 平衡、功率因数、
受电限制、经济目标、储能反馈和通信流程保持原语义。
无设备时求和为零；按设备/母线 ID 映射变量，不依赖两个字典的顺序相同。

## 适用范围与证据

模型用于离线协调计划，不表示现场授权或真实执行。
协调层仍使用聚合网络和校准损耗，不证明支路电压/容量可行；
ADMM 参考与区域申报间也存在数值残差。MISOCP、独立 AC 及逐点跟踪检查
继续决定能否采用；分钟实际出力、动态响应和滚动完成度分别评价。

解析例：P=3 MW 时，山城 k=.30 得到 |Q|≤.9 Mvar，一般 k=.333 得到
|Q|≤.999 Mvar；若绝对限值为 .4 Mvar，则两者均受 .4 限制。
若将风机采用 P 限发至 1 MW，即使预测为 4 MW，其山城 Q 上限也只有 .3 Mvar。
四边容量多边形给出可手算的 `|P|,|Q|≤S/√2`，独立验证 MVA 装配。
SVG 声明 [−.2,.4] Mvar、S=.3 MVA 时有效区间为 [−.2,.3] Mvar。

真实 SDK 边界测试见 `tests/integration/dispatch/test_coordination_capabilities.py`；
原输入不变、两种风机比例及两类执行优化器测试一并验证。
新增约束均为仿射等式/不等式，保留凸二次模型，并以 `is_dcp()` / `is_qp()`
检查表达形式。方法依据：[CVXPY DCP 文档](https://www.cvxpy.org/tutorial/dcp/index.html)。
