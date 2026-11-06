# 单微电网现场接口第二项：UpdatePlanningInputs

本接口更新已有影子会话的预测、边界、电价和费用资料。它区分“资料已检查并暂存”和“已重新校核并生效”，不会启动完整经济调度仿真、推进设备状态或下发现场指令。

## 调用方式

```text
POST /api/field/single-microgrid/planning-inputs
Content-Type: application/json
```

```python
from pathlib import Path
from oilfield_energy.bootstrap.field_initialization import create_planning_updater
from oilfield_energy.workflows.field_initialization.contracts import UpdatePlanningRequest

request = UpdatePlanningRequest.model_validate_json(Path("planning-update.json").read_bytes())
service = create_planning_updater(Path("artifacts/field-initialization"))
response = service.execute(request)
print(response.model_dump_json(indent=2))
```

存储根目录必须与 InitializeRun 相同。默认 HTTP 服务使用 `artifacts/field-initialization/sessions.sqlite3`；指定 `create_app(root=...)` 时使用 `<root>/field-initialization/sessions.sqlite3`。时钟默认采用真实 UTC，不要通过固定时钟绕过现场时效检查。

请求使用共同信封，`operation=UpdatePlanningInputs`、`run_id` 必填；`payload.context_id` 为当前生效上下文。正文提交**完整的一批**预测、电价、费用和原储能任务，不是局部补丁。字段以 [请求模式](../../contracts/field_initialization/planning-update.request.schema.json) 和 [响应模式](../../contracts/field_initialization/planning-update.response.schema.json) 为准。现有服务的 `/docs` 也包含接口。

## 接纳与生效

| 结果 | 含义 |
| --- | --- |
| HTTP 200 / `activated` | 在已有、对应时刻且仍有效的确认状态上完成设备、观测边界、PCC、网络、安全续接和储能任务校核，已原子切换到新上下文 |
| HTTP 202 / `staged` | 资料检查和运行域映射通过，但缺少对应生效时刻的有效确认状态；只保存候选资料 |
| HTTP 409 / `rejected` | 会话、上下文、版本、任务、资料覆盖或校核不满足条件；不改变当前生效上下文 |
| HTTP 422 / 413 / 503 | 分别表示消息字段或 JSON 无效、正文超过 8 MiB、会话存储不可用 |

`staged` 的 `new_context_id/evidence_ref` 为 null，`energy_task_guarantee=not_assessed`。`activated` 返回新上下文、证据引用和实际校核结果，但仍有 `mode=shadow`、`dispatch_enabled=false`。没有储能任务时为 `not_required`；有任务且获证为 `maintained`；可选任务未获证为 `unproven`。强制任务未获证不能生效。

**当前 SubmitSnapshot 已能接收、校核和保存观察，ReportExecution 尚未实现。** 可用于生效的确认状态仍只有初始化快照，因此必须满足 `effective_from == snapshot_at`，请求处理仍处于该快照的一分钟边界区间内，而且量测尚未过期。即便放宽量测最大年龄，也不能把上一分钟状态当成这一分钟状态。完成校核后再次核对时刻，跨过边界时不提交生效结果。新快照若设置了持久化观察阻断标记，合格资料只暂存并返回 `RESYNC_REQUIRED` 原因，不能基于旧状态生效。

未来时刻的资料可暂存；缺少对应确认状态或快照过期时同样暂存并返回原因。暂存不是安全证明，也不会因为时间到达而自动生效。未来快照和控制接口需要补齐暂存资料与确认状态的绑定、重新校核及切换流程。本阶段没有独立的“激活暂存版本”端点；重放暂存请求只返回原响应。

## 输入与不变量

- 新版本绑定当前 `context_id`；每个会话的 `input_revision` 不可重复使用已接纳或已暂存的版本，也不能复用初始化版本。已拒绝的版本未被占用，但修正内容必须使用新的 `message_id`。
- 生成时间不晚于当前时刻，也不晚于申请生效时刻；生成时间和生效时间不能倒退到当前版本之前。请求创建时间受原控制策略的时效限制。
- 生效时刻和预测点在一分钟网格上；预测首末点分别等于 `effective_from/valid_until`。每点完整覆盖原模型母线和风光资源。
- 负荷区间、风光下界、装机上限、插值声明、电价覆盖均实际检查。按原初始化策略至少覆盖 90 分钟、规划窗口和响应期限，并覆盖原储能任务截止时刻；最多 24 小时。资料缺失时不生成模拟默认值。
- `terminal_boundary_policy=last_bounds_until_valid_until` 和来源引用仍必填；有效期不因末点延续或历史响应重放而延长。
- 储能任务整体保持一致，包括 ID、绝对期限、能量上下限、`required` 和来源。禁止删除、增加或替换任务；任务变更须另行设计明确的接纳流程。
- 网络、设备能力、点表、控制策略和权限沿用当前会话，不能通过此接口修改。权限须覆盖当前时刻及申请生效边界；结果有效终点不超过权限有效终点。
- 已接纳外部 PCC 目标时，生效前会重新校核原 P/Q、原响应期限和误差带，且不得缩短原保证终点；失败则拒绝更新。输入与目标证据原子切换，`preserved_target_deadline_at` 返回原期限；没有原目标时为 null。详见 [第三项说明](Field_Pcc_Targets.md)。
- 电价、费用仍使用当前算法支持的非负元/MWh 口径。生效只更新输入及校核证据，不代表完成经济优化、产生设备指令或取得现场实绩。

## 持久化、并发与恢复

数据库增加 `planning_updates` 和 `planning_heads`，保留原初始化表及历史证据不变。前者保存请求、内容摘要、原响应、占用版本和生效证据；后者指向本会话当前生效的更新。没有更新时读取原初始化证据。旧版仅含初始化表的数据库会自动进行增量建表。

检查旧上下文、保存新证据、记录响应和切换当前版本位于同一事务中。并发提交两个基于同一旧上下文的生效请求，只能有一个成功；另一个报告上下文变化。中途失败会整体回滚，不留下只有响应或只有上下文的新版本。

消息 ID 在初始化和更新之间共用。同微电网、同消息 ID、相同内容返回原响应；不同内容或跨操作复用 ID 返回 `DUPLICATE_ID_CONFLICT`。重启后仍然如此。历史 `activated` 响应只证明当时的结果，不应当作当前状态查询；历史 `staged` 响应也不会被改写为已生效。

`compute_budget_seconds` 继续作为计算后的接纳门槛，暂存校验也检查预算；不是强制中止计算的硬实时超时。SQLite 锁等待不包含在数值计算预算中，存储暂不可用时使用原消息重试。拒绝或暂存更新不会自动延长旧证据的有效期限。

## 验证范围

```text
python -m pytest tests/workflows/test_field_planning_updates.py tests/integration/service/test_field_planning_http.py
python tools/export_field_initialization_contracts.py --check
```

测试使用小型解析网络进行真实校核，另覆盖时间边界、严格任务继承、版本冲突、并发、重启、旧库迁移、事务回滚和 HTTP 行为。不运行完整调度仿真，不把软件检查等同于现场联调。

整体接口边界见 [实际场景接口说明](../禁止做任何改动的文档/新能源最优协同控制算法_实际场景接口说明.md)。第三项外部 PCC 目标已实现影子接纳；第四项 [SubmitSnapshot](Field_Snapshots.md) 已实现观察接入，但不会自动激活暂存资料。第五项 [PrepareControlCycle](Field_Control_Cycles.md) 已实现影子准备；规划实际激活会原子作废旧影子批次，暂存不会。执行反馈及后续接口尚未实现。
