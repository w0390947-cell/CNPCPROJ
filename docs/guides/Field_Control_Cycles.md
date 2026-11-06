# 单微电网现场接口第五项：PrepareControlCycle

本接口利用当前已获证策略准备一个 60 秒周期的影子指令，保存设备响应预测、设备变化范围及网络校核证据。成功为 `shadow_only`，拒绝为 `withheld`。不会下发命令、推进已确认设备状态或把预测能量当作现场实测能量。

## 调用方式

```text
POST /api/field/single-microgrid/control-cycles/prepare
Content-Type: application/json
```

```python
from pathlib import Path
from oilfield_energy.bootstrap.field_initialization import create_control_cycle_preparer
from oilfield_energy.workflows.field_initialization.contracts import PrepareControlCycleRequest

request = PrepareControlCycleRequest.model_validate_json(Path("cycle.json").read_bytes())
service = create_control_cycle_preparer(Path("artifacts/field-initialization"))
response = service.execute(request)
print(response.model_dump_json(indent=2))
```

与前四项使用同一存储根目录；HTTP 服务使用 `<root>/field-initialization`。默认采用实际 UTC 时钟，测试可注入时钟。请求沿用共同信封，`operation=PrepareControlCycle`，正文为：

| 字段 | 当前要求 |
| --- | --- |
| `context_id` | 当前已激活上下文 |
| `cycle_id` | 会话内不可变周期身份；修正内容需使用新 ID |
| `cycle_start_at` | 当前仍新鲜的初始化确认边界，同时等于生效预测起点 |
| `cycle_end_at` | 起点后恰好 60 秒 |
| `snapshot_id` | 当前上下文最新观察的 ID；没有新观察时使用初始化快照 ID |
| `authority` | 与初始化权限对象完全一致，且 `write_enabled=false` |

完整字段见 [请求模式](../../contracts/field_initialization/control-cycle.request.schema.json)、[响应模式](../../contracts/field_initialization/control-cycle.response.schema.json)。

## 策略与确认边界

1. 校验会话、上下文、权限、完整分钟覆盖、快照质量及时效。不能绕过新快照而指定旧快照。
2. 核对观察的设备 P/Q/E 与已确认初始化状态一致，并重新检查当前负荷、风光可用量及固定状态 AC/PCC。观察阻断标记存在时返回 `RESYNC_REQUIRED`。
3. 有已接纳外部 PCC 目标时，使用其获证反馈策略，保留原目标、原误差带、原响应期限、原保证终点及原储能任务。目标证据不适用时拒绝准备，不能丢弃目标后改用无目标策略。
4. 无外部目标时，使用已保存的安全续接或终态策略；适用时使用储能能量回归策略。必需的储能任务没有续接证据时拒绝准备。
5. 调用共享的 `single_execution.api.prepare_certified_cycle()`，计算指令、预测 P/Q/E、设备变化包络、路径及终点 AC 证据，并核对反馈/能量任务管束。准备函数不修改控制器或现场确认状态。
6. 计算后复核预算与确认时效，再原子保存响应、影子批次和证据。预算超限后拒绝接纳结果，尚非硬实时抢占。

目前只有初始化状态可作为确认状态。因此本项不能连续完成现场第二分钟及以后周期，也不负责清除观察阻断标记、激活暂存规划或接入新的 15 分钟经济滚动求解。`ReportExecution` 及后续重同步工作仍待实施。现有仿真 `advance()` 已复用共享准备函数，然后由原控制器提交模拟状态；现场接口不调用它。

## 输出及时间含义

HTTP 200 表示影子准备完成，409 表示业务拒绝或 ID 冲突，422/413/503 分别表示输入不符合契约、超过 8 MiB、存储不可用。

成功结果携带不可变 `command_batch_id`，每条命令带 `command_id` 和 `resource_id`。只对获准轴输出数值；未获准轴为 null，不能用零代替。完全没有获准轴的资源不会生成命令项，但仍包含在设备状态预测中。此处输出规范化资源指令，不将已声明调节权限视为真实点号写入链已经可用。

如果整个模型都没有获准控制轴，则返回 `NO_AUTHORIZED_COMMANDS`，不创建空指令批次。

`predicted_endpoint` 单独记录设备 P/Q 和储能能量；内部证据同时保存输入快照、运行域、原策略、设备变化包络、网络证据及下一步期望范围。它们不是执行记录。响应始终包含 `mode=shadow`、`dispatch_enabled=false`、`execution_confirmed=false`。

`apply_at` 是本次**同步模型计算**的起始边界，`cycle_end_at` 是模型周期末，`time_basis=synchronous_model_only`。即使请求在边界后数秒到达，也仅在数据仍新鲜时作影子计算，绝不据此声称命令曾按该边界实际送达。`dispatch_deadline_at`、`feedback_due_at` 均为 null，未授予现场发送或确认期限。`model_check.guaranteed_until` 为当前一分钟的模型校核终点，条件包括声明输入范围、精确状态和同步响应假设。

`strategy_source` 为 `retained_feedback/backup/terminal`；`energy_feedback` 单独指示是否使用能量回归。存在外部目标时还返回 `target_response_deadline_at`、`target_guaranteed_until`；存在储能任务时返回 `energy_task_deadline_at`，均不重置。此增量不产生 `economic` 或 `dispatchable` 结果。

拒绝结果没有批次、命令或预测，`apply_at/cycle_end_at=null`，`model_check.status=unproven`，并返回具体错误。未取得证据不等于已证明所有物理策略均不可行。

## 去重、并发和失效

五项接口共享 `(microgrid_id, message_id)` 命名空间。同消息同内容返回原响应，异内容或跨操作冲突返回 `DUPLICATE_ID_CONFLICT`。同会话同 `cycle_id` 绑定正文（UTC 归一化时间），不同正文返回 `CYCLE_ID_CONFLICT`；同正文换消息 ID 返回原结果及新消息关联，不重新计算。拒绝记录也绑定周期 ID。

每个会话只保留一个当前影子批次；另一个新周期请求返回 `CYCLE_ALREADY_PREPARED`。新有序快照进入当前观察头、外部目标接纳、规划版本激活时，在相同事务内记录旧批次失效并清除当前批次指针。暂存规划、拒绝目标、历史快照重放不会清除指针。使用新上下文或新快照重新准备时必须另用周期 ID，并仍满足初始化确认边界。

历史请求、响应和证据不修改。失效后或过期后重放旧成功响应，表示重放历史结果，不能恢复当前指针、延长有效期或作为现场下发授权。查询接口尚未实现；当前批次及失效记录已持久化供后续查询/反馈设计使用。影子批次不是已发送、等待现场反馈的动作。

SQLite 使用共享写事务串行处理准备及输入变更，批次保存或失效失败会整体回滚。新增表采用增量建表，既有初始化/规划/目标/快照证据不重写。回退到旧程序会忽略批次失效机制，不应在已有批次会话上混用新旧服务；回滚前应停用该会话并按部署备份恢复。

## 验证

```text
python -m pytest tests/workflows/test_field_control_cycles.py tests/integration/service/test_field_control_cycles_http.py tests/contracts/test_field_initialization_contracts.py
python tools/export_field_initialization_contracts.py --check
```

覆盖小型解析网络的一分钟预测、与原控制器单步结果一致、原期限及轴权限、状态不推进、失败关闭、重复/并发/重启、变更失效及事务回滚。未运行完整场景调度仿真，未进行 D5000 或现场通信验证。
