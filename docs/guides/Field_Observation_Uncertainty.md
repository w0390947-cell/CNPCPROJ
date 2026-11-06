# 单微电网量测误差与时标不确定性接口

本增量继续落实《新能源最优协同控制算法实际场景接口说明》中的反馈、量测质量、能量证据和异常恢复要求。原文档保持不变；本说明补充已有接口的实现约定。

维护负责人：仿真研究维护负责人；评审职责：优化调度维护负责人、设备控制维护负责人。

## 接纳方式与信任边界

显式启用 `control_policy.bounded_timing.profile = bounded_uncertain_v1`、`response_semantics = monotone_no_overshoot_bounded_settling`、`model_error_policy_ref = bounded-observation-v1`，并提供 `control_policy.observation_uncertainty`。其 profile 为 `bounded_observation_v1`。原同步模式、`bounded_monotone_v1` 和默认无发送端口的装配继续保留原语义；它们不会静默忽略新增误差字段。

这里的误差是经过接纳的确定性绝对界，不是标准差或置信区间。策略必须包含 revision、来源、有效期、clock_revision、边界采样半径、根节点电压范围，以及覆盖全部标量点位且不重复的 `measurements`。每个 `MeasurementLimit` 声明：

| 字段 | 含义 |
| --- | --- |
| source_ref / source_kind | 固定点位与 measured 或 estimated 类型 |
| calibration_revision | 仪表校准或估计误差模型版本 |
| max_absolute_error | 该规范化量单位下的绝对误差界 |
| max_timestamp_error_seconds | 原始采样时间相对服务参考 UTC 的误差界 |
| alignment_rate_per_second | 采样点到分钟边界之间，该物理量的最大每秒变化量 |
| provenance_ref | 校准、变化率界的可追溯来源 |

闭环通信端口必须预配置完整策略的 `timing_policy_sha256` 和 `observation_policy_sha256`，并匹配 profile/revision。使用公开的 `api.policy_digest(approved_policy)` 对已经独立审核的 DTO 计算摘要。该函数使用系统规范序列化；集合顺序及时间表示属于被绑定内容。不能在生产端口中直接从本次初始化请求复制策略并自动表示信任。摘要绑定内容，不替代校准、授权或设备响应验证。

服务接收时间、消息封装时间、分钟网格、权限有效期和控制期限仍采用可信服务参考 UTC；本增量建模的是原始量测、设备事件和恢复隔离证据相对这一参考时钟的偏差。尚未提供服务自身时钟失效或跳变的容错协议。

## 原始量测与边界状态

每个 `MeasuredValue` 继续保留原始 value、observed_at、quality、source_kind、source_ref 和 instantaneous 语义；新增 uncertainty。例如下面是结构示例，数值不能作为现场标定参数：

```json
{
  "absolute_error": 0.005,
  "calibration_revision": "approved-meter-1",
  "provenance_ref": "calibration-record:17",
  "valid_from": "2026-11-05T00:00:00Z",
  "valid_until": "2026-11-06T00:00:00Z",
  "timestamp": {
    "error_seconds": 0.1,
    "clock_revision": "approved-clock-1",
    "provenance_ref": "clock-test:9",
    "valid_from": "2026-11-05T00:00:00Z",
    "valid_until": "2026-11-06T00:00:00Z"
  }
}
```

报文中误差界必须等于该点位已接纳的界，版本和 source_kind 必须一致，不能自行报更小误差以缩窄状态。证据有效期须覆盖整个采样时间区间和当前决策时刻；缺项、过期、版本变化、点位不完整均拒绝。

原始量测范围为 `[value-error, value+error]`。对普通标量，用接纳的变化率乘以采样时间区间到分钟边界的最坏时间差，向两侧扩展。完整采样时间区间必须落在 `boundary_radius_seconds` 内。原始和对齐范围分别保存在快照评估 `measurement_ranges`、`aligned_ranges`；不修改原始采样时标或数值。

设备状态还可以与独立的保持响应证据求交；执行确认时，在全部采用事实成立之后，可以再与原批次物理可达端点求交。交集为空表示证据矛盾。设备限值、安全带、PCC 目标带和能量任务范围只用于核验，不能用于裁剪观测状态。

`SubmitSnapshot` 返回 `observed_box` 和 `state_semantics=bounded_set`，只表示观测评估。只有完成原批次全部执行及量测核验的 `ReportExecution` 才推进确认状态。查询响应的 `confirmed_state_box` 是后续周期使用的确认集合；`confirmed_state=null`，`representative_state` 仅作为明确标识的代表值，`state_evidence_ref` 标识依据。内部原策略可以用代表值生成候选，但必须另对整个集合证明，不能以候选预测提交实际完成状态。

## 保持历史与响应误差

`held_setpoints` 必须提供实际采用的设定记录，保留原始读回时间 `observed_at`，并新增 `timestamp_uncertainty`、`held_since`、`since_uncertainty`。它表示同一设定从 held_since 持续保持到读回时刻。最晚可能的采用时间加最大稳定时间，必须早于分钟边界和该设备最早可能的采样时间；最早可能的读回时间必须晚于边界和全部设备样本的最晚可能时间。只给当前设定或最后发送指令不能证明这段历史。

`ResourceTiming` 的 `settled_p_error_mw`、`settled_q_error_mvar` 是独立设备响应残差，与仪表误差分别接纳。若声明 `zero_p_is_exact` 或 `zero_q_is_exact`，必须另有可核实的零设定精确关断条件；系统不会自动把跨过零的状态裁剪到零。旧精确模式不接纳非零残差。

本阶段仍要求设备在稳定区间内有界保持、采用后单调且无超调，且在下一边界的完整采样窗口之前稳定。储能有功仍为已知的即时 ZOH，settling 和有功残差必须为零；不支持未知 PCS 动态或任意过渡期采样。

## 能量、执行时间与顺序

在稳定采样窗口，PCS 的已知保持功率为 p。使用模型充放电效率计算每秒能量变化率 r(p)，将能量原始样本对齐到边界：

`E_boundary ∈ E_sample_range + r(p) × (boundary − actual_sample_time_range)`。

此处用有理数时长和向外舍入；接纳的能量变化率界还必须覆盖实际 r(p)。周期可达能量继续覆盖全部应用时间，并扩展到名义应用窗口两端各加减全局时钟误差，不以最后指令的一分钟积分冒充实绩。

`EnergyEvidence` 引用原始能量点位，energy_end_mwh 和 uncertainty_mwh 必须等于原始样本值和校准误差，`sample_observed_at` 必须保留原采样时标；interval_start/end 仍标识被确认的控制分钟。measured 点位使用 `method=measured_energy`，明确接纳的 estimated 点位使用 `method=bounded_estimate`。本系统未实现 SOC 换算或功率积分估计器；上游估计须独立验证绝对误差，并保留 estimated 类型和来源。

设备回执 `CommandResult.timestamp_uncertainty` 同时约束原始确认/采用时标。整个时标区间须满足发送、接收、应用窗口及当前时刻的顺序约束；多设备最晚可能采用与最早可能采用之差不得超过 skew。原始回执及其时钟证据不可变。名义时标在界内而完整区间越界仍阻断。发送期限额外扣除时钟余量，物理可达证明使用 `physical_apply_earliest_at/latest_at`。

点位水位保存完整实际时间下上界，后一个样本的最早时刻不得早于前一个样本的最晚时刻。仅名义时间递增但区间重叠不能证明顺序。重复报文、历史回执、查询对账、部分执行和停止沿用既有事务及不自动重发规则。

离散 online / remote_enabled / blocked 状态也必须带 `DeviceState.status_timestamp_uncertainty`，其版本、完整时间范围、采样边界及点位水位同样核验；不把未校准的远端状态时标当作精确 UTC。离散状态值仍须明确有效，不支持概率化的权限或在线状态。

## 整集合安全、目标与任务

负荷和风光可用量的完整对齐范围必须支持已声明的预测包络，所有设备能力、路径、有限续接和原期限均对整个状态集合检查。有限范围及条件仍显式保留；矩形外包保守拒绝不表示设备一定不安全。

`root_voltage_bounds_pu` 是独立接纳、在有限证明时域内持续有效的根节点物理电压包络，不是用当前仪表样本推测的未来范围。整个对齐 PCC 电压范围必须包含于该包络；AC 校核也必须证明包络中所有输入电压、负荷和设备状态。`radial-ac-disc-v2` 将根节点作为不确定参数，用统一自映射/压缩证明传播各 tap 后的电压、支路和 PCC 包络，保留 `slack_voltage_input_pu`；不只检查中点或有限角点。原固定根节点证书继续为 v1。

PCC 模型与量测范围有交叠只证明模型一致性；设备/电网安全和实际目标带分别对完整集合检查。量测中心在目标带内、但范围跨越边界，返回 `TARGET_TRACKING_UNPROVEN`；两范围分离返回 `TARGET_TRACKING_VIOLATED`。不以模型预测范围替代实际 PCC 跟踪核验。

能量任务到期时，确认能量范围全部在原任务带内为 met，完全分离为 missed，部分交叠为 indeterminate。required 任务的未来证明必须覆盖整个能量集合；可选任务未证明时仍明确返回 unproven。只有确认的 met 历史才解除已过期任务，不通过恢复补写过去实绩。

## 恢复、有效期与兼容

沿用 PrepareResynchronization / ResumeRun。逐命令终结、隔离代次及新边界快照均保留时钟误差；终结/隔离的最晚时刻必须早于后续事实的最早时刻。准备保持阻断，恢复时重新核对快照、策略、权限、控制修订、全状态集合和原目标/任务期限，原失败批次继续保留。隔离之后可能发生的旧动作仍会重新阻断。

初始化、预测更新和恢复有效期同时受量测策略有效期约束。本阶段不能在线替换量测/时钟策略；到期会拒绝新控制，不能用预测更新或恢复延长旧校准。新校准版本的在线切换是后续范围。

公开入口仍为 InitializeRun、UpdatePlanningInputs、RequestPccTarget、SubmitSnapshot、PrepareControlCycle、DispatchControlCycle、ReportExecution、QueryRunStatus、StopRun、PrepareResynchronization、ResumeRun。严格 JSON Schema/OpenAPI 由统一生成器导出。新旧程序不能同时写同一个活动数据库。

本次完成软件合同、集合计算和生命周期验证；不包含 D5000 适配、现场仪表校准或响应标定，也不新增后台调度器、服务时钟故障协议或无限失联保持保证。安全证明以接纳的物理、误差、扰动和时间前提为条件，本地保护及 fallback 仍须按原接口职责实施。

```powershell
.\.venv\Scripts\python.exe -m pytest tests/workflows/test_field_uncertainty.py tests/integration/service/test_field_uncertainty_http.py tests/unit/power_flow/test_ac_envelope.py -q
.\.venv\Scripts\python.exe tools/export_field_initialization_contracts.py --check
.\.venv\Scripts\python.exe tools/export_economic_contracts.py --check
```

测试采用独立有偏量测与解析响应，不从被测预测端点伪造实测。覆盖持续周期、误差/校准拒绝、整区间越界、目标与任务歧义、时序与恢复隔离、估计能量溯源、预测切换、事务回滚和重启重放；AC 输入区间另用独立高精度两节点解和带 tap/shunt 的节点 Newton 解核对。
