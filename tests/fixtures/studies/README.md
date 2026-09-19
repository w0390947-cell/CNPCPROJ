# 历史采用计划样例

`legacy_adopted_schedule.json` 为第十项修复前实际运行的 8 时段/H4 模拟归档，来源 `artifacts/audit_20260918/item10/local_baseline/results/executed_interval_plan.json`。项目自产模拟数据，无现场数据。

样例故意保留错误范围：设备计划 8 段，而 cluster 与求解证书仅属于首个 4 段窗口。历史读取测试必须保留原值，不可把它重新认证为完整时域结果，也不可用新生成结果覆盖此样例。
