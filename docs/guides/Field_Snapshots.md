# 单微电网现场接口第四项：SubmitSnapshot

本接口接收规范化现场快照，校核量测、声明条件和固定运行点，保存观察事实及证据。它不调用设备执行模拟器，不确认执行周期，不替换已确认设备状态，不激活暂存规划。当前仍为影子模式。

## 调用方式

```text
POST /api/field/single-microgrid/snapshots
Content-Type: application/json
```

```python
from pathlib import Path
from oilfield_energy.bootstrap.field_initialization import create_snapshot_submitter
from oilfield_energy.workflows.field_initialization.contracts import SubmitSnapshotRequest

request = SubmitSnapshotRequest.model_validate_json(Path("snapshot.json").read_bytes())
service = create_snapshot_submitter(Path("artifacts/field-initialization"))
response = service.execute(request)
print(response.model_dump_json(indent=2))
```

使用与前三项接口相同的存储根目录；HTTP `create_app(root=...)` 使用 `<root>/field-initialization`。默认时钟为真实 UTC；测试可注入时钟。

请求使用共同信封，`operation=SubmitSnapshot`，`run_id`、`microgrid_id` 指向已有会话。`payload` 是完整快照，其 `context_id` 必须为当前生效上下文。初始化内嵌快照仍必须使用 `context_id=null`，不会因新增本接口而放宽。

完整结构见 [请求模式](../../contracts/field_initialization/snapshot.request.schema.json)、[响应模式](../../contracts/field_initialization/snapshot.response.schema.json) 和 [接口总说明](../禁止做任何改动的文档/新能源最优协同控制算法_实际场景接口说明.md) 第 6 节。

## 数据及校核

每次提交全部母线纯负荷 P/Q、全部设备实际 P/Q 和状态、储能实际 E、全部风光可用量、PCC P/Q/电压及版本信息。当前根电压固定为 1.0 pu，因此 `voltage_pu` 必填。设备 P/Q 按向网内注入为正，PCC P/Q 按上级电网输入为正；单位分别为 MW、Mvar、MWh。

量测保留原采样时标、质量与来源；不填零、不裁剪、不从设定值推算实际状态或储能能量。当前继续采用初始化时的精确规范化点值约定：负荷、设备 P/Q/E 和 PCC 需要实测瞬时值，风光可用量允许明确标识的估计值。尚不支持带误差范围的状态估计，也不新增 D5000 原始报文、SOC 换算或通信采集。

检查顺序如下：

1. 确认会话、上下文、分钟边界、消息时间及快照顺序。
2. 按显式 ID 核对完整性、唯一性、点表、纯负荷范围、质量、同步程度、状态与控制权限；不得按数组顺序猜设备身份。
3. 使用 `snapshot_at - planning_inputs.effective_from` 对应的分钟索引，检查当时的负荷预测区间及风光可用量下界。只使用当前生效预测，暂存资料不参与放行；超出有效期不重复使用最后预测。
4. 映射实际 P/Q/E，检查设备能力和已有获证运行域；以未修改的负荷与设备注入做固定状态 AC 校核，并使用独立的 PCC 模型比较容差核对实测 PCC。
5. 若当前目标有覆盖该时刻的跟踪状态范围，检查实际状态是否落在其中；在原响应期限之后、保证终点之前，检查实测 PCC 是否位于原申请误差带。此项只评价观察事实，不确认指令执行。
6. 计算完成再次检查时效与计算预算，持久化结果。

风光的获证设备上限可能低于装机或当前可发功率，因为既有证明按声明可用量下界构造。超出该保守范围会使原证据不再适用，不等于设备物理上绝对不可运行。网络“未获证”同样不自动等于已经证明物理不安全，具体原因保留在结果中。

模型、映射、权限、运行方式及当前 `operating_state_ref` 须保持与会话一致。本阶段无法解释新的运行记录是否改变了开关、投退或分接头；引用改变时要求重新同步，不在线修改网络。

## 响应语义

| 字段或状态 | 实际含义 |
| --- | --- |
| HTTP 200 / `outcome=accepted` | 该快照的输入与当前时刻校核通过；不表示执行成功或未来保证恢复 |
| HTTP 409 / `outcome=rejected` | 输入、身份、时序、模型或校核条件未通过；可解析的业务请求通常仍归档 |
| HTTP 422 / 413 / 503 | 分别为报文无效、超过 8 MiB、持久化失败；不返回成功确认 |
| `input_status=valid/invalid` | 是否通过完整性、来源、质量、时效、版本及许可等输入检查；格式正确不等于 valid |
| `state_status=consistent` | 当前观察点通过本次模型及适用证据检查，不表示预测轨迹已执行 |
| `state_status=outside_model` | 状态、声明条件、权限/版本、网络证据或目标跟踪检查不满足，详见错误码 |
| `state_status=unconfirmed` | 缺少可用量测、有效覆盖，或因时效/计算预算等原因不能完成确认 |
| `observation_status=latest` | 本次推进观测顺序；它也可能是无效或越界观测，不是最后已确认执行状态 |
| `observation_status=archived` | 已保存此次请求，但因乱序、旧上下文、错误会话或消息时间等原因不推进观测顺序 |
| `observation_status=rejected` | 消息或快照 ID 冲突等阻止建立新记录 |
| `guarantee_status=invalidated` | 记录到原保证前提不满足的证据；原证书保留为历史，不再作为当前放行依据 |
| `guarantee_status=unconfirmed` | 本操作未确认执行及后续保证，不能据此声称原保证已恢复 |
| `guarantee_status=not_assessed` | 未能评价对应会话的保证状态 |
| `resync_required` | 是否存在持久化的重新同步要求 |
| `evaluated_at`、`evidence_ref`、`errors` | 原评估时刻、历史记录引用及具体错误；错误也列于共同响应信封 |

响应始终包含 `mode=shadow`、`dispatch_enabled=false`、`execution_confirmed=false`。

## 新观察与旧保证的关系

最新、属于当前上下文的快照若质量无效或违反声明条件，会设置持久化的观察阻断标记。即便快照本身模型一致，只要它处于新的分钟边界，或者实际设备 P/Q/E 已与原确认状态不同，也需要重新同步；接收观察不自动把它提升为完成状态。

标记生效后，新 `RequestPccTarget` 请求返回 `RESYNC_REQUIRED`；`UpdatePlanningInputs` 对结构合格的资料仍可暂存，但不能使用旧确认状态生效。既有目标的原截止时间、储能任务期限、历史证书和原响应均不改写。通过门槛的同边界、同设备状态观测不会凭空建立此标记。

后续正常快照可以成为最新的模型一致观测，但不能自动清除阻断标记。`invalidated` 不会被后续 `unconfirmed` 降级覆盖；结果中可能同时出现 `state_status=consistent` 与 `resync_required=true`，两者分别描述当前观察和会话恢复条件。

当前仍只有初始化状态可用于前三项中的确认边界判断。新确认状态的提交、阻断标记的解除、暂存规划与新状态绑定、原任务续接等，留给后续显式重新同步和控制/反馈流程实现。本接口不能单独使会话持续闭环运行。

## 身份、时序及事务

同一会话当前只支持一条适配层汇聚后的快照序列；多数据源须先由适配层组装。`sequence` 严格递增，`snapshot_at` 不倒退；同一分钟允许更高序号的修订观察。各量测与状态点按 ID 保存已见采样时标，后续时标不得倒退；中间快照缺少某个点，不会抹去该点此前的时标。缺失或无效字段不用于伪造正常值；之前无效的未来采样时标不会成为不可恢复的时标门槛。

消息 ID 在四项接口间共用。相同消息与内容跨重启返回原响应，内容不同或跨操作复用报 `DUPLICATE_ID_CONFLICT`。快照 ID 在会话内不可变，初始化快照 ID 已被保留；改变快照内容必须使用新的快照 ID 和消息 ID。已归档的拒绝快照同样绑定身份。

同一快照以新消息 ID 重发时返回历史评估内容并关联新消息，不重新计算或推进观测顺序。快照身份将时间统一为 UTC，并按母线/设备 ID 排序，等价时区或数组排列不改变快照身份。历史重放保留原 `evaluated_at`、状态和证据引用，不代表查询了最新会话状态，也不延长有效期。

新增 SQLite 表 `snapshot_requests` 保存原请求、评估及响应；`snapshot_heads` 保存最新按序观测及各点时标，`snapshot_valid_heads` 单独保存最新输入有效且模型一致的观测；两者均不是已确认执行状态。`observation_barriers` 保存需重新同步的阻断依据。四项接口共用事务边界，保证记录、观测指针和阻断标记一起提交或整体回滚；不重写初始化、规划和目标历史。

计算预算是结果接纳门槛，不能强制抢占数值计算，不构成硬实时性能承诺。异常事实会归档，但因计算超时或量测变旧而不能宣布通过。

## 验证与后续工作

```text
python -m pytest tests/workflows/test_field_snapshots.py tests/integration/service/test_field_snapshots_http.py
python tools/export_field_initialization_contracts.py --check
```

测试采用解析小网络和固定时钟，覆盖真实固定状态校核、时序、异常值、去重、并发、回滚、重启、旧库增量建表及前三项联动；不运行完整调度仿真。第五项 `PrepareControlCycle`、第六项 `ReportExecution`、查询与停止接口仍待逐项确认后开发。

第五项 [PrepareControlCycle](Field_Control_Cycles.md) 已实现单分钟影子准备。新有序快照进入观察头时，会在同一事务中作废此前的当前影子批次；历史重放不会复活或清除批次。观察与执行确认仍分开。
