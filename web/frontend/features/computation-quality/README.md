# 求解质量展示

负责人：前端维护负责人；复核：优化调度维护负责人。

公开入口 `index.ts` 导出 `ComputationQualityDetails`，由页面组合器用于单微网，
由集群执行面板通过公开入口复用。仅依赖生成的质量/执行契约，不请求数据，
不计算最优性或安全结论。单微网模式只显示优化证据，不显示 ADMM 信息。
历史缺失质量记录由调用者保持缺失；预算耗尽、异常与达到精度分别显示。

依据：`docs/modeling/Computation_Quality.md`；生命周期验证位于
`tests/components/dashboard-result-identity.test.mjs`。
