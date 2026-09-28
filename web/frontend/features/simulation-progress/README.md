# 仿真任务进度

负责人：前端维护负责人；复核：仿真研究维护负责人。

公开入口 `index.ts`。仅依赖生成的作业状态契约，将服务端阶段和窗口完成数
转换为显示文本和流程百分比；没有计时器、求解规则或校核判据。

阶段开始对应该阶段区间的起点，滚动窗口完成比例填充第 4 阶段区间。
百分比保留一位小数，不表示耗时比例，不估算剩余时间。
100% 要求 `succeeded` 且 `result_available`；完成不代表校核通过。
取消、失败、中断、连接不可确认时只展示最后记录，不继续描述为正在执行。
旧任务没有 `rolling` 时保留阶段进度；无阶段记录时不补造完成量。

测试：`web/frontend/tests/simulation-progress.test.mjs`、
`web/frontend/tests/components/dashboard-result-identity.test.mjs`。
