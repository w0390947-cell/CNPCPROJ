# 单微电网现场接口第一项：InitializeRun

当前实现提供初始化应用接口与 HTTP 入口，调用真实的网络、设备、安全续接和储能任务校核，并保存初始化会话。它支持 `shadow` 模式；不会模拟推进设备状态，也不会向现场下发指令。第二项资料更新、第三项目标接纳和第四项快照接入已实现，见 [UpdatePlanningInputs](Field_Planning_Updates.md)、[RequestPccTarget](Field_Pcc_Targets.md) 与 [SubmitSnapshot](Field_Snapshots.md) 使用说明；第五项 [PrepareControlCycle](Field_Control_Cycles.md) 已实现单分钟影子准备，其余三项逻辑接口尚未实现。新快照不会自动替换初始化的确认状态。

## 调用入口

```text
POST /api/field/single-microgrid/initialize
Content-Type: application/json
```

现有 FastAPI 服务的 `/docs` 与 `/openapi.json` 包含实际模型。准备规范化现场请求后可提交：

```powershell
Invoke-RestMethod -Method Post `
  -Uri 'http://127.0.0.1:8000/api/field/single-microgrid/initialize' `
  -ContentType 'application/json' -InFile '.\initialize.json'
```

Python 调用同一个应用用例，不需要启动 HTTP 服务：

```python
from pathlib import Path
from oilfield_energy.bootstrap.field_initialization import create_field_initializer
from oilfield_energy.workflows.field_initialization.contracts import InitializeRequest

request = InitializeRequest.model_validate_json(Path("initialize.json").read_bytes())
service = create_field_initializer(Path("artifacts/field-initialization"))
response = service.execute(request)
print(response.model_dump_json(indent=2))
```

默认注入真实 UTC 时钟；固定时钟仅用于测试，不应借此绕过现场过期检查。完整字段以 [请求模式](../../contracts/field_initialization/initialize.request.schema.json) 和 [响应模式](../../contracts/field_initialization/initialize.response.schema.json) 为准。[两母线测试输入](../../tests/support/field_initialization.py)中的审批及量测明确为解析测试材料，不构成真实现场审批。

## 第一阶段支持范围

- `operation=InitializeRun`，接口版本为 `single-microgrid-field-v0.1-draft`，请求 `run_id/correlation_id` 及内嵌快照、预测的 `context_id` 为 null。
- 仅接纳 `shadow`；`closed_loop` 返回 `CLOSED_LOOP_UNAVAILABLE`，`authority.write_enabled` 只能为 false。
- 网络对象使用现有 `NetworkModelV2`，检查批准元数据、支路来源、运行方式和径向连通性。审批字段属于外部声明，服务不独立认证审批人身份。
- 最多 512 个母线、128 个资源，一个储能模型。不可控非零注入、非保持式储能有功及逐设备不同的无功动态暂不支持。
- 输入量测已经按点表归一化。入口核对单位、对象、source_ref 和计量范围，不重新乘倍率或反号，也不读取 D5000。
- 设备、负荷、PCC 和储能能量使用质量有效的瞬时实测值，风光可发量可明确标记为估计值。当前不直接支持平均量、SOC 或设备/负荷状态的不确定性区间。
- 初始化要求 `pcc.voltage_pu=1.0`，与当前固定根电压模型一致。`model_error_policy_ref=exact-normalized-snapshot-v1` 表示模型假设，不是现场测量误差已经被解决的声明。
- PCC 模型对比容差 `pcc_model_tolerance_mw/mvar` 必填，与目标跟踪容差分开。
- 纯负荷范围使用 `scope_ref/bus_id/members/semantics/provenance_ref`，`semantics=gross_load`，成员不能重复。
- 点表量枚举为 `p/q/energy/voltage/online/remote_enabled/blocked/p_setpoint/q_setpoint`，方向规则为 `identity/negate`，归一化单位为 `MW/Mvar/MWh/pu/1`。写点仅作为声明保存，不执行写入。
- 一分钟网格；预测首点与初态同刻、末点与 `valid_until` 同刻。至少覆盖 90 分钟、规划窗口及响应期限，最多 24 小时。按请求显式边界及 `linear/hold` 规则映射，不回退为模拟误差参数。
- 必填 `terminal_boundary_policy=last_bounds_until_valid_until` 和 `terminal_boundary_ref`，证据不授权超过有效期运行。
- 电价为非负元/MWh，区间无缝覆盖预测范围。当前不支持负电价或新增结算科目。

## 返回与持久化

HTTP 200 表示影子初始化接纳；409 表示业务拒绝或冲突；422 表示消息格式/字段无效；413 表示请求超过 8 MiB；503 表示存储不可用。格式错误返回边界诊断对象，可解析请求的业务拒绝返回标准信封和 `payload.errors`。

成功响应包含会话/上下文 ID、`mode=shadow`、`run_state=ready`、`dispatch_enabled=false`、版本、安全及储能任务结果、证据引用和有效期限。ready 仅表示影子初始化通过。

SQLite 原子保存原请求、解析后的模型/点表、运行域、初态、安全续接、储能任务证据和响应。`create_app(root=...)` 使用 `<root>/field-initialization/sessions.sqlite3`，默认位置为 `artifacts/field-initialization/sessions.sqlite3`，连接按请求关闭。

网络或点表可引用 `{artifact_id, revision, sha256}`，文件从该根目录的 `objects/<sha256>.json` 读取并校验摘要。标签不作为文件路径，不联网下载。解析后的资料随会话保存，不依赖原对象永久存在。

同一微电网和消息 ID 的相同请求重放原响应，支持并发及进程重启；不同内容返回 `DUPLICATE_ID_CONFLICT`。历史重放不会延长 `valid_until`。不同消息 ID 试图替换已初始化微电网返回 `ACTIVE_RUN_EXISTS`；停止、重新同步和重置留待后续接口，不通过删库冒充恢复流程。

强制储能任务未获共同证明返回 `TASK_UNPROVEN`，不创建 ready 会话；可选任务未获证保留 unproven 和原因。计算超预算不提交会话；`compute_budget_seconds` 是计算结束后的接纳门槛，不强制中断数值校核，也不包含 SQLite 排队等待。

## 验证与后续边界

专项测试覆盖真实 AC/任务证明、坏量测、映射、输入越界、必需/可选任务、摘要、初态不推进、超预算、并发/重启和 ASGI 入口。生成契约检查命令为 `python tools/export_field_initialization_contracts.py --check`。

本次直接从规范化输入构造运行域，没有修改 `FieldCaseBuilder.build()` 的历史工作簿导入行为。预测生成、在线快照更新、经济规划触发、指令准备/下发、执行反馈、查询和停止都未在此接口中实现。

详见 [总接口说明](../禁止做任何改动的文档/新能源最优协同控制算法_实际场景接口说明.md)。
