# 无功计划余量、动态过渡与 PCC 反馈

模型标识：规划 `reactive-planning-v1`（`DISPATCH-Q-001`），执行 `reactive-tracking-v1`（`CONTROL-Q-001`）。负责人：优化调度、设备控制维护负责人；复核：潮流计算维护负责人。见 [ADR 0030](../architecture/decisions/0030-reactive-tracking.md)。用于离线仿真，不增加现场写点权限。

## 问题与职责

SVG 已满载、风机命令已到上限时，静态计划可行也不能保证过渡按时完成。旧分钟流程按稳态补偿量分配命令，安全修正器只在不安全时寻找安全动作，没有进一步最小化经过设备动态后的 PCC 无功误差。

规划规则位于 `dispatch.application_reactive`，反馈搜索位于 `control.application_reactive_tracking`。旧数值桥接负责求解器表达、状态投影、权限仲裁和 AC 评价。PCC 的 Q 正值为无功受电，设备 Q 正值为容性注入，增大注入通常降低 PCC 受电。功率单位为 MW/Mvar，时长为分钟。

## 规划约束

`ExecutionPolicy.reactive_planning` 保存版本、来源和参数。默认预留无功能力的 10%，过渡规划范围为 5 分钟，实际采用 `h=min(优化时段长度, 5 分钟)`。这两个参数是待现场校准的软件假设。时间常数 τ 和总无功爬坡沿用同一 `dynamic_tracking` 策略。规划范围不是新的验收期限。

设资源物理线性能力面为 `aP+bQ≤c`。风机使用视在容量内接多边形、绝对 Q 上限及 `|Q|≤kP` 的交集；光伏、储能必须有无功权限；SVG 使用实际调度限额。原物理参数不改。

令 `u=1-reserve_fraction`，添加 `aP+bQ/u≤c`，在固定 P 下预留 Q 空间。无权限设备的 Q=0 原约束保留。该规则适用于现有包含零无功运行点的能力包络。

令 `d=exp(−h/τ)`、`α=1−d`。规划过渡需存在物理允许的恒定命令，使 `Q=d Qprevious+α Qcommand`，即添加：

```text
a P(t) + b Q(t)/α − b d Qprevious/α ≤ c
|Q(t) − Qprevious| ≤ 总无功爬坡额度 × h / 获准参与资源数
```

资源等分总爬坡预算，保守保证绝对变化量之和不超额。首时段 Qprevious 来自上一已执行分钟检查点，后续时段来自相邻计划。没有检查点的首次启动不补造实测值，只约束静态余量及后续过渡。实际状态在预留范围外也不篡改；无法满足约束时保留无解/不可采用状态。

纯规则生成不可变线性约束行，协调凸模型、MISOCP、线性后端共用。CVXPY 使用稀疏矩阵和参数化右端，窗口间刷新反馈不保留旧状态。区域采用前，用计划 P/Q 独立重算约束残差；原设备、网络和跟踪约束不变。日内规划启用该约束，日前经济参考保持原语义。

该过渡包络按计划 P 估计无功能力，不是整个变化 P 轨迹和非线性网络的动态证书；分钟执行仍独立验证。

## 分钟闭环

安全修正器先取得满足权限仲裁、设备动态及 AC 安全约束的基准动作，再通过只读预测端口改进跟踪：

1. 将候选命令交给控制器副本仲裁，不能消耗正式序号或补造执行确认。
2. 始终从同一个周期初实际 Q 推演一次一阶响应与总绝对爬坡限幅，不在搜索中反复推进物理状态。
3. 使用本分钟实际 P、负荷和网络计算 AC，检查电压、线路、PCC 受电、功率因数以及仲裁后总量是否被接受。
4. 正 PCC 无功误差增加容性注入，负误差减少；按同方向剩余授权命令空间分配，并用 α 反算命令增量。
5. 候选不安全或不改善误差时缩小步长；最多 4 轮，每轮最多 4 次回退。只采用安全且严格改善的候选。

动作目标精度 1e-4 Mvar、变化比较 1e-9 Mvar 只控制搜索，不替代 0.05 Mvar 工程限值。饱和、拒绝或网络限制时保留剩余误差。原 best_effort/held/unavailable 安全路径不会被搜索改成通过。

最终命令须经正式仲裁并与预测投影一致，随后执行一次物理响应和最终网络校核。原 PCC 目标、历史响应和校核结果不改写。

## 证据与兼容

`policy.reactive_planning` 记录规划策略，`policy.reactive_tracking_version` 标记控制器版本。`rolling_updates.initial_reactive_mvar` 保存真实初始设备 Q；`REACTIVE_PLAN_ENVELOPE` 是独立计划约束检查。

分钟详细网络记录 `reactive_control.tracking` 保存基准/最终预测误差、仲裁前命令、仲裁后目标、预测响应及候选次数，不将预测冒充实绩。实际响应单独保存。

历史策略字段缺省 None、初始 Q 证据缺省空，不补造证书。显式关闭无功规划不关闭物理约束；新计算启用执行修正。响应期限算法、10 分钟最大等待、连续确认规则和工程偏差限值不变。

## 验证入口

- `tests/unit/control/test_reactive_tracking.py`：双向跟踪、饱和、单周期爬坡、无效/不安全候选。
- `tests/unit/dispatch/test_reactive_planning.py`：解析过渡、预留与权限。
- `tests/integration/dispatch/test_reactive_planning.py`：三个后端一致性。
- `tests/integration/dispatch/test_coordination_workspace.py`：反馈参数刷新和模型复用。
- `tests/integration/control/test_reactive_tracking.py`：真实 AC、仲裁拒绝、预测与执行响应一致。
- 原连续设备会话、储能、保护、集群及历史契约回归；全天复算见 `artifacts/reactive-tracking-20260925/`。
