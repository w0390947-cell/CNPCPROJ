# 单微电网现场接口第三项：RequestPccTarget

本接口接收外部调度给定的 PCC 有功、无功目标，在已有影子会话上进行固定目标校核，返回接纳或拒绝。成功只表示声明条件下取得了策略证据；当前不向设备下发指令，也不推进设备状态。

## 调用方式

```text
POST /api/field/single-microgrid/pcc-target
Content-Type: application/json
```

```python
from pathlib import Path
from oilfield_energy.bootstrap.field_initialization import create_pcc_target_requester
from oilfield_energy.workflows.field_initialization.contracts import RequestPccTargetRequest

request = RequestPccTargetRequest.model_validate_json(Path("pcc-target.json").read_bytes())
service = create_pcc_target_requester(Path("artifacts/field-initialization"))
response = service.execute(request)
print(response.model_dump_json(indent=2))
```

使用与 InitializeRun、UpdatePlanningInputs 相同的存储根目录。HTTP 默认根目录为 `artifacts/field-initialization`；`create_app(root=...)` 对应 `<root>/field-initialization`。默认时钟为真实 UTC，固定时钟仅用于测试。

共同信封中 `operation=RequestPccTarget`，绑定已有 `run_id`、`microgrid_id`，正文 `context_id` 必须为当前生效上下文。实际字段见 [请求模式](../../contracts/field_initialization/pcc-target.request.schema.json)、[响应模式](../../contracts/field_initialization/pcc-target.response.schema.json)，或现有服务的 `/docs`。

## 当前约定

- `target_id` 标识不可变目标；正文完整提供 P/Q、来源、生效时刻、绝对响应期限、保持终点和两项误差带。功率单位为 MW/Mvar，P/Q 按 PCC 输入方向定义。
- 当前只有初始化快照可用于确认状态。目标生效时刻须等于该快照和当前运行域的起点，处理请求时仍在同一分钟内，并通过量测时效、质量、权限及当前证据有效期检查。未来生效或缺少有效确认状态的请求明确拒绝，不进入队列。
- 三个目标时刻必须精确落在一分钟网格，不对请求时间取整。要求 `effective_from < response_deadline_at < hold_until`，保证至少覆盖响应期限及其后一个完整保持分钟。当前不支持“生效瞬间必须已达标”的零响应期限。
- 正数误差带可比配置更严格，不能比原控制策略更宽。校核域实际使用申请的误差带，不能扩大误差后接纳目标。
- 固定 P/Q 不作替换。目标超出配置范围或现有构造无法取得跟踪、设备、网络、安全续接及强制储能任务的共同证据时拒绝。“未获证”不等于已经证明所有物理策略均不可行。
- 原储能任务、绝对截止时刻、权限和模型沿用当前会话，不由目标请求改写。

接口直接调用带明确截止步数的公开固定目标校核入口，绕开会按默认响应时长推导期限的内部经济接纳流程。策略使用当前确认状态和现有安全计划作为构造参考，没有调用经济调度仿真或设备执行循环。

## 保证期限与结果

| 返回 | 含义 |
| --- | --- |
| HTTP 200 / `accepted` | 原 P/Q、原绝对截止时间及申请误差带通过共同校核，已保存策略证据 |
| HTTP 409 / `rejected` | 会话、版本、身份、时间、误差带、数据覆盖或校核条件不满足；原目标记录不被覆盖 |
| HTTP 422 / 413 / 503 | 分别表示消息字段或 JSON 无效、正文超过 8 MiB、会话存储不可用 |

响应包含 `requested_target`、`accepted_target`、原 `response_deadline_at`、`guaranteed_until`、证据引用、储能任务保证及原目标状态；拒绝时 `accepted_target/guaranteed_until/evidence_ref` 为 null。成功保持 `mode=shadow`、`dispatch_enabled=false`。

`guaranteed_until` 取申请保持终点、输入证据有效期、预测终点与权限终点共同覆盖的完整分钟。权限若在分钟中间到期，只保证此前完整分钟。它可能早于 `hold_until`，调用者必须依据明确返回的保证终点安排后续续接；本接口不自动续期。保证范围不足以覆盖截止时刻及其后一分钟时拒绝。本阶段尝试这一声明覆盖范围，不搜索所有更短期限。

储能任务保证分别为：无任务 `not_required`，与目标共同获证 `maintained`，可选任务未获证 `unproven`；拒绝请求为 `not_assessed`。强制任务未获证不会接纳目标。

`previous_target_id` 指向决策前所选目标；`previous_strategy_status` 解释其证据范围：

| 值 | 含义 |
| --- | --- |
| `none` | 已读取会话，该会话没有原目标 |
| `conditional` | 原上下文、有效期限和当前确认状态仍满足检查；结论依赖声明条件，不代表设备已执行 |
| `expired` | 原目标保证或当前输入证据已经到期 |
| `context_changed` | 原目标证据没有绑定当前上下文 |
| `unconfirmed` | 缺少当前边界的有效确认状态，无法继续声称原保证有效 |
| `not_assessed` | 如消息 ID 冲突等在读取会话前即被拒绝，未评价原目标 |

新目标只有获证并成功提交后才替换当前目标。上述原策略状态评价的是决策时的证据条件，不表示替换成功后仍同时执行两个目标。

## 重发、版本切换与事务

消息 ID 与前两项共用：同微电网、同消息 ID、同内容跨重启返回原响应；内容不同或跨操作复用时报 `DUPLICATE_ID_CONFLICT`。历史响应不能替代当前状态查询，其重放不延长保证有效期。

同会话、同 `target_id` 绑定完整目标义务，只有 `context_id` 可随重新校核变化；P/Q、来源、期限、保持终点和误差带变化须使用新目标 ID。已记录的拒绝请求同样绑定该 ID，修正义务需要新目标 ID 和新消息 ID。重发当前相同 P/Q，即使另换目标 ID，也不能重置原响应期限。

预测更新生效前，必须重新校核当前外部目标的原 P/Q、原截止时间、原误差带及储能任务，保证终点不能比原已获保证更早。无法续接时拒绝预测更新，保留原上下文与目标。暂存新预测不更换目标；真正生效时，`preserved_target_deadline_at` 返回原绝对截止时间，目标保证来自新域下的共同校核。

SQLite 增量建立 `pcc_target_requests`、`pcc_target_heads`、`pcc_target_revalidations`。请求及响应、目标证据、当前目标指针在同一事务内提交。预测生效则把新输入证据、输入指针、新目标证据和目标指针共同提交；任一写入失败整体回滚。原初始化、旧预测和旧目标响应保留为历史记录。与预测更新并发时，不会留下指向旧上下文的当前目标证据。

`compute_budget_seconds` 是计算结束后的接纳门槛，计算结束还会检查确认边界与输入时效；超预算或跨过确认边界时不接纳。它不承诺强制中止计算或现场硬实时响应。

## 验证与后续边界

```text
python -m pytest tests/workflows/test_field_pcc_targets.py tests/integration/service/test_field_targets_http.py
python tools/export_field_initialization_contracts.py --check
```

测试覆盖固定目标与截止时间、严格误差带、部分保证、任务继承、拒绝与旧目标保留、预测更新续接、并发、回滚、重启、旧库增量建表及 HTTP 坏消息。使用小型解析网络的真实数学校核，不运行完整调度仿真。

第四项 [SubmitSnapshot](Field_Snapshots.md) 已实现快照接收、校核、归档及保证失效标记。它不提交新的已确认执行状态；出现观察阻断标记后，新目标申请返回 `RESYNC_REQUIRED`，后续正常快照不能自动恢复原保证。第五项 [PrepareControlCycle](Field_Control_Cycles.md) 已实现影子指令准备；新目标接纳会原子作废旧影子批次。执行反馈、查询和停止接口尚未实现。现场完整闭环仍需后续开发和现场资料支持，详见 [总接口说明](../禁止做任何改动的文档/新能源最优协同控制算法_实际场景接口说明.md)。
