# 单微电网软件执行闭环

本增量依据实际场景接口草案第 9～12、15 节，补齐软件侧执行反馈、确认状态推进、查询、停止与可注入通信端口。未修改 `docs/禁止做任何改动的文档/` 内的设计原件。

维护负责人：仿真研究维护负责人；评审职责：优化调度维护负责人、设备控制维护负责人。

## 使用范围

- 默认服务仍无通信端口，保持影子运行，不会连接 D5000 或真实设备。
- 原闭环支持显式注入的 `software_synchronous_v1` 端口和虚拟同步时钟；新增有界延迟及异步执行模式见[有界时序接口](Field_Bounded_Timing.md)，两种模型条件分别接纳，测量误差仍未覆盖。
- 单网固定径向模型、根电压 1 pu、一个等效储能及精确瞬时量测等原限制保持不变。
- 本增量复用已有外部目标反馈、安全续接、储能回归与终态策略；没有新增现场经济滚动求解器或后台自动调度器。
- 异常保持 `resync_required`；恢复须通过新增的两步显式接口，见[重新同步与恢复](Field_Recovery.md)，正常快照或晚到回执仍不能自行解除阻断。

## Python 装配

公共类型从 `workflows.field_initialization.contracts` 导入；用例及 `ExecutionTransport` 从 `workflows.field_initialization.api` 导入。

`bootstrap.field_initialization` 提供：

```python
initializer = create_field_initializer(root, clock=clock, transport=transport)
preparer = create_control_cycle_preparer(root, clock=clock, transport=transport)
reporter, status_query, stopper, dispatcher = create_execution_services(
    root, clock=clock, transport=transport,
)
```

所有服务共享同一存储目录与时钟。注入的端口需要实现：

| 成员 | 契约 |
| --- | --- |
| `capabilities` | `TransportCapabilities(timing_profile="software_synchronous_v1", provenance_ref=...)` |
| `authorized(microgrid_id, authority)` | 向可信权限来源核实 owner、epoch、有效期与许可；不能仅相信请求自报 |
| `send(microgrid_id, batch)` | 接收不可变 `ExecutionBatch`，返回已知的 `CommandResult` 元组；不得伪造设备采用或量测 |
| `query(microgrid_id, batch)` | 按原批次/命令 ID 查询已知结果；不重发指令 |

本增量没有提供可写真实设备的默认适配器。软件测试中的 `tests/support/field_execution.py` 是明确标注的解析测试替身，生产代码不得导入它。

闭环初始化要求 `mode=closed_loop`、`authority.write_enabled=true`、所有授权写轴有明确写点，以及 `dispatch_lead_seconds=0`。缺少注入端口或权限核对失败时拒绝接纳。端口移除后，已有闭环会话也不能继续准备或下发。

## 正常周期

1. 从已确认边界调用 `PrepareControlCycle`，返回 `dispatchable` 批次、预测与证书；此时尚未发送或推进状态。
2. 在同一个虚拟模型边界调用 `DispatchControlCycle`。软件同步模型要求应用边界精确一致，晚到动作拒绝发送。
3. 发送前先保存 `sending` 声明，再调用端口。响应 `pending` 只确认该声明，不能解释为设备执行成功；端口返回的事实另行归档。
4. 用 `ReportExecution` 记录传输/采用阶段回执。缺少采用证据时仍为 pending。
5. 周期末通过 `SubmitSnapshot` 提交完整实测快照。正常关联的周期末观察不会自行确认执行，也不会被当作无关联状态变化直接阻断。
6. 通过 `ReportExecution(feedback_stage=cycle_end)` 引用该快照及实测能量证据。全部核验通过后原子提交实测确认状态，释放当前批次。
7. 从新确认状态准备下一分钟。策略位置按实际绝对时钟推进；原 PCC 期限、保证终点及储能任务期限不重置。

预测端点始终单独保存在批次证据中。现场确认不调用仿真控制器 `advance()`，也不以预测值覆盖实测值。现有仿真仍按原模拟执行方式运行。

## HTTP 入口

所有请求使用既有消息信封、严格 JSON 和带时区时间。

| 操作 | POST 路径 | 请求模式 |
| --- | --- | --- |
| `DispatchControlCycle` | `/api/field/single-microgrid/control-cycles/dispatch` | `dispatch-cycle.request.schema.json` |
| `ReportExecution` | `/api/field/single-microgrid/execution-reports` | `execution-report.request.schema.json` |
| `QueryRunStatus` | `/api/field/single-microgrid/status` | `run-status.request.schema.json` |
| `StopRun` | `/api/field/single-microgrid/stop` | `stop-run.request.schema.json` |

模式位于 `contracts/field_initialization/`，通过 `tools/export_field_initialization_contracts.py` 生成。200 表示本操作接纳，202 表示待确认，409 表示业务拒绝；422/413/503 分别表示报文无效、超过 8 MiB、存储不可用。执行操作统一使用 `ExecutionResponse`，其中 `cycle_status` 和逐设备结果与响应 `outcome` 分开读取。

`create_app(..., field_transport=..., field_clock=...)` 允许显式装配软件端口和共同虚拟时钟，但不默认创建真实通信连接。接口部署仍需要可信调用方、访问控制和隔离；本增量没有增加登录认证。

## 反馈及能量

`ReportExecution` 按原 context、cycle、batch 和 command ID 关联。设备采用设定值不等于实际出力已达到目标；回显与实测 P/Q 分别保存。已知结果不能被后续 unknown 降级或被矛盾结果静默覆盖。

本阶段周期确认要求：

- 全部授权命令均有传输接收及设备采用证据，应用时间等于获证边界，回显与授权设定一致。
- 周期末引用最新完整快照，数据、版本、设备状态和网络/目标校核通过，实际 P/Q/E 位于后续证书范围。
- 在 `feedback_due_at` 前完成确认，且不超过计算预算；时间/预算是接纳条件，不是实时抢占保证。
- 储能 `energy_evidence.method=measured_energy`，区间与本周期一致，来源关联储能能量点，数值与快照一致，`uncertainty_mwh=0`。SOC 转换、实测功率积分和非零误差须后续扩展验证，当前不能用命令积分代替。

`tracking_status` 明确评价最后确认边界；`energy_task_outcome` 使用已确认边界历史，到期结果保存后不因后来周期而重写。未确认周期的观察事实通过快照与执行记录追溯，不能冒充新的确认状态。

## 查询、停止与异常

`QueryRunStatus.payload.command_batch_id` 可选；省略时查询当前会话及未决批次。`reconcile=true` 会调用端口查询并归档事实，不调用 send，也不自动确认周期或解除阻断。查询的传输 I/O 在存储事务之外，结果绑定查询开始时的原批次，避免串到后来批次。

每次需要新状态时使用新 `message_id`；重复 ID 返回历史响应，不能当实时查询。历史确认回执不会覆盖较新的完成状态。已知拒绝、回显冲突、应用时差、能量或状态越界及超时都会阻断后续控制。发送或反馈期限在查询、发送、反馈等操作进入时检查；外部运行管理应按周期主动查询，当前没有独立后台超时调度器。

发送声明提交后发生崩溃，实际动作可能发生也可能未发生；重启只允许查询和对账，绝不靠重放请求重发。一个会话同时只保留一组待确认动作。部分执行不进行虚假的整批回滚。

`StopRun` 不要求旧 context；由端口核实当前管理权限后停止新发送声明，取消未发送批次。已经声明发送的批次视为在途，即使设备调用尚处于崩溃窗口，也继续保留并接收反馈。停止不生成零功率命令，反馈确认也不把 stopped 恢复成 ready。

预测更新只在对应的实测确认边界重新校核后生效，保留原任务和目标期限。存在未决动作或阻断时只允许合格资料暂存。暂存版本仍不自动激活；可在取得有效确认边界后提交新的明确版本申请，不能通过刷新资料解除异常阻断。

## 存储与回退

SQLite 增加 `execution_sessions`、`execution_batches`、`execution_requests`。发送声明、确认状态和消息历史遵循事务边界；外部发送不可能与 SQLite 形成物理原子事务，所以崩溃后保留未知状态。原初始化及请求证据不覆写，确认状态作为独立记录和只读投影使用。

禁止旧版与新版进程同时操作活动会话。回退前停止服务并备份数据库；旧代码不理解新的执行状态，不能通过换回旧代码恢复未决动作。同一会话的异常恢复已通过显式接口提供，新会话替换仍未开放。

## 验证

```powershell
.\.venv\Scripts\python.exe -m pytest tests/workflows/test_field_execution.py tests/integration/service/test_field_execution_http.py -q
.\.venv\Scripts\python.exe tools/export_field_initialization_contracts.py --check
```

测试包括独立解析反馈下的连续运行、非零 P/Q、原目标及储能期限、后续确认边界上的预测更新、部分/未知执行、迟到、重复、并发、崩溃、重启、停止及持久化回滚。测试结果限于软件验证，不代表真实 D5000 联调、现场安全或硬实时验收。
