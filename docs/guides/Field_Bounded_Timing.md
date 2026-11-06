# 单微电网有界延迟与异步执行接口

本增量依据《新能源最优协同控制算法实际场景接口说明》第 9～12 节，特别是第 11.1 节，增加 `bounded_monotone_v1`。它允许取数、计算、发送和设备采用发生在不同时间，使用独立时间响应条件重新校核，不从一分钟 alpha 推算秒级响应。禁止改动目录内的原设计文档保持不变。

维护负责人：仿真研究维护负责人；评审职责：优化调度维护负责人、设备控制维护负责人。

## 模型与接入条件

调用方在 `control_policy.bounded_timing` 提供显式配置；省略时保留原同步模式。闭环端口必须声明相同 `timing_profile` 和 `timing_revision`，并独立核实当前控制权限。默认服务仍不创建真实通信连接。

新模型要求：每个分钟边界的实测 P/Q 已稳定在已知保持设定；新设定采用后，P/Q 单调、无超调，并在声明的最大时间内精确稳定到规范化设定。每台设备、每个轴可先后变化；矩形包络覆盖全部次序。储能有功是即时设定并保持的 ZOH，储能条目的 settling_max_seconds 必须为零。本阶段储能无功也要求即时稳定。

这些条件是需要设备响应验证和来源记录的输入前提，不是本系统从回执或 alpha 推导的事实。原 alpha、规划和反馈策略用于生成候选指令；候选还必须通过独立的有界时间校核。不能满足单调稳定条件的设备不接纳该模式。

本阶段保持精确规范化瞬时量测、共同精确时钟、固定径向拓扑和一分钟网格。`clock_error_bound_seconds` 必须显式为 0。非零时钟误差、测量误差、渐变 PCS 有功、超调、振荡和任意连续时间模型尚不支持。有限稳定时间不等于由现场联调验证；本次验证范围为软件。

## 时间配置

以下秒数只用于解析软件测试，不能直接用作现场参数：

```json
{
  "profile": "bounded_monotone_v1",
  "revision": "analytic-timing-1",
  "provenance_ref": "analytic-bounds-only",
  "response_semantics": "monotone_no_overshoot_exact_settling",
  "clock_error_bound_seconds": 0.0,
  "prepare_max_seconds": 7.0,
  "transport_max_seconds": 2.0,
  "apply_not_before_seconds": 10.0,
  "apply_not_after_seconds": 16.0,
  "application_skew_max_seconds": 4.0,
  "snapshot_delivery_max_seconds": 2.0,
  "resources": [
    {"resource_id": "wind", "settling_max_seconds": 8.0, "provenance_ref": "analytic-wind"},
    {"resource_id": "storage", "settling_max_seconds": 0.0, "provenance_ref": "analytic-pcs"}
  ]
}
```

设分钟边界为 t，各时间都相对同一边界：

| 字段或时刻 | 约定 |
| --- | --- |
| snapshot_at | 仍为 t，不能改成收到报文的时间 |
| received_at | 保留实际接收时间，且减去 t 不大于 snapshot_delivery_max_seconds |
| prepare_deadline_at | t + prepare_max_seconds；计算完成后再次检查 |
| dispatch_deadline_at | t + apply_not_before_seconds − transport_max_seconds |
| apply_not_before_at / apply_not_after_at | 全批设备允许采用的绝对时间窗口 |
| apply_at | 返回窗口左端，表示最早允许应用，不声称所有设备同时在此刻应用 |
| cycle_end_at | t + 60 秒，所有设备必须已稳定 |
| feedback_due_at | cycle_end_at + feedback_timeout_seconds |

配置必须满足 prepare_max + transport_max ≤ apply_not_before，apply_not_before ≤ apply_not_after，且每台设备 apply_not_after + settling_max < 60。最大应用时差不超过窗口宽度。还要求 snapshot_delivery_max ≤ feedback_timeout，feedback_timeout + compute_budget ≤ prepare_max，为下一周期确认及计算预留时间。dispatch_lead_seconds 保留为 0，所有新延迟由上述显式窗口表达。

时间边界使用微秒精度；更细的配置明确拒绝，能量区间使用与报文时间完全相同的有理数时长，避免小数秒被时间格式舍入后改变保证。

示例周期：边界 00 秒采样，01 秒收到，03 秒完成准备并发送；12、14 秒两台设备先后采用；60 秒采样，61 秒收到并确认，再在下一分钟的允许窗口内准备和发送。全过程保留一组待确认动作，不增加流水执行。

## 保持设定与实测状态

初始、周期末和恢复快照都必须包含 `held_setpoints`，每个资源恰好一项：

```json
{
  "resource_id": "storage",
  "p_mw": 0.1,
  "q_mvar": 0.0,
  "observed_at": "2026-10-15T02:00:00Z",
  "source_ref": "controller:verified-held-setpoint"
}
```

保持设定来自控制系统实际采用的设定记录，不能从本系统最后发出的命令猜测。其时标和设备 P/Q/E 采样时标须等于 snapshot_at，P/Q 须与稳定实测一致。条目缺失、重复、身份错误或尚未稳定均拒绝。原始事实继续归档；拒绝不会裁剪量测。

## 数值校核与有限续接

校核从实测确认状态出发，对每台设备构造旧设定到新设定的独立区间，检查整个矩形范围的容量、Q/P、变化额度与 AC/PCC 安全，覆盖设备、P/Q 轴的全部应用次序。它可能比同步相关路径更保守，拒绝不等于实际必然不安全。

储能使用旧、新实际保持功率及应用窗口。令 r(p) 为按充放电效率计算的 MWh/秒变化率，实际采用偏移为 d，则：

`E_end = E_start + r(p_old) × d + r(p_new) × (60 − d)`。

在窗口两端计算外包能量范围；过程能量额外包括初始点及切换时刻，覆盖先放后充等反向动作。全部边界使用现有有向舍入区间，不以新的整分钟指令积分冒充实绩。确认仍需要周期末实测能量证据。

候选尾部来自已有规划、目标反馈或能量回归策略，按代表状态生成固定指令序列，再逐分钟对所有延迟和显式负荷、风光包络重新校核。这里只把原策略当候选生成器。每一步都验证路径、下一边界、原目标误差带和绝对期限、 required 储能任务，以及有限覆盖内到达终态保持。未来负荷反馈的同步证明不直接作为异步保证。

timed_proof.energy_task_guarantee 独立记录异步模型下的任务保证。required=false 的可选任务未取得时间证明时允许继续安全控制，但必须返回 unproven，不能复用同步证明标成 maintained。

`timed_proof` 保存本周期路径与端点区间、完整有限续接指令/端点、时间窗口、profile_revision、guaranteed_until 和摘要。predicted_endpoint 中的能量是该区间的名义中点，不能作为实际完成状态。无法取得新候选时可对上次已保存的续接重新校核；没有有效续接时拒绝准备，不沿用一条旧指令无限保持。

准备、目标接纳、预测生效及恢复都重新校核时间条件。预测切换可保留已经过去的原目标期限，其相对新预测起点的 deadline_step 可以为负，绝对期限不变。已确认完成的储能任务继续保留实绩，不因进入后续分钟而再次要求完成过期任务。

上述有限续接以后续动作仍按约定执行、扰动仍在声明包络内为条件。失联后保持旧设定超过已覆盖等待区间的安全性不由该证明保证，应执行已定义的 local_fallback_policy；本系统不伪造物理回滚或替代本地保护。

## 发送与反馈接口

沿用既有 InitializeRun、PrepareControlCycle、DispatchControlCycle、SubmitSnapshot、ReportExecution、QueryRunStatus、StopRun、PrepareResynchronization 和 ResumeRun，无新增厂商接口。请求和响应模式仍由统一生成器导出。

端口 `send(grid, batch)` 必须读取 batch.evidence.timed_proof 中的窗口，按该窗口安排应用，拒绝过期写入，并维持权限代次隔离。send 返回表示已知事实；仅接收或排程时返回 device_status=unknown，实际采用事实可稍后通过 ReportExecution 补交。提供 send 接口并不意味立即写入设备。本项目不内置 D5000 协议或通信适配。

发送声明先提交，再调用端口。调用超出 transport_max 时保留未知并阻断；查询不会重发。未取得传输接受证据的超时取原 transport_timeout 与 transport_max 的较小值。开始调用前再次检查时间和权限。

实际应用时间必须在原批次窗口内，逐设备采用时间的极差不得超过 application_skew_max。回执可以在实际应用之后到达；在反馈有效期内补报按时发生的动作可以确认，实际越过窗口的动作不能因回执较早或重放而放行。回显、全部设备采用证据、完整实测状态、端点区间、PCC 跟踪及实测能量仍分别检查。

报告时保留原 context/cycle/batch/command 标识；下一周期以新实测状态开始。异常、迟到、部分拒绝、重复、停止和崩溃仍沿用现有事务与对账规则。恢复可在新的实测边界后的准备窗口内完成，不再要求零耗时；候选有效期也受该窗口限制。

状态响应 validation_scope 为 bounded_monotone_v1；原软件同步模式仍为 software_synchronous_only。prepared/pending/accepted 均不替代 cycle_status=confirmed。

## 验证与兼容

```powershell
.\.venv\Scripts\python.exe -m pytest tests/workflows/test_field_timing.py tests/integration/service/test_field_timing_http.py -q
.\.venv\Scripts\python.exe tools/export_field_initialization_contracts.py --check
```

测试包括独立解析设备反馈下的连续延迟周期、先后应用、迟到回执与迟到动作区别、矩形中间状态越界、充放电反向与能量窗口、任务不可达、原期限及预测切换、恢复续跑、发送超时、计算越界和严格 HTTP 契约。

新增字段默认空值，旧会话继续使用原模式；新模式必须显式接纳。旧可执行文件无法解释新增时序证据和保持设定，活动数据库不允许新旧版本混写。运行管理仍须主动调用接口并监视期限，没有新增后台自动调度器或硬实时抢占器。
