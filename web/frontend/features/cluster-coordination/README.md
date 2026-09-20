# 集群协调回放

负责人：前端维护负责人；评审：仿真研究维护负责人。

公开入口 `index.ts` 暴露 `ClusterCoordinationPanel` 和 `CoordinationMode`。
负责逐轮申报/参考、区域信息状态与最终 PCC 计划展示，不求解、不判定安全、
不发请求、不修改结果。现有仪表盘是页面组合器，拥有作业和主回放游标；
功能内仅保存观察计划时刻。切换模式暂停播放，过程从末轮开始，计划从首时刻开始。

依赖：React、图标库、shared 区域名称与由 Python 权威契约生成的
`shared/api/generated/coordination.ts`；样式使用本功能 CSS Module。
不依赖其他功能包或旧的 lib 传输模型。功率差仅是展示差值，不是安全校核。

不变量：算法轮次与日内时刻独立；按区域 ID 关联数据；计划按原始点回放；
P/Q 正值为 PCC 受电；过程中的申报是协调器最近接受的方案，缺失不填零；
fresh 表示当前协调批次内仍有效的响应，可能是之前通信轮次缓冲的报文。
失联与本地降级可同时存在。未收敛结果标为最后一轮参考，不称为执行实绩。

兼容：旧结果 `coordination` 缺失或 null 时明确提示未记录逐轮功率，
仍可读取 `cluster_timeseries` 查看最终计划；不从最终值补造过程。
记录协议和内存边界见 [ADR 0014](../../../../docs/architecture/decisions/0014-coordination-replay.md)。

测试：`node --test tests/components/dashboard-result-identity.test.mjs` 使用真实 React
生命周期和合成传输夹具，覆盖完成/恢复末轮、双时间轴、回放、旧结果、未收敛、
故障状态及最终计划禁止注入迭代事件；这些测试不声称验证数值算法或浏览器布局。
同时运行 `tsc --noEmit`、受影响文件 lint 和生产构建。
