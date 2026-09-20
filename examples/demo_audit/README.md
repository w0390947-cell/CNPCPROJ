# Excel 审计测试资料

原合成工作簿已归入 [tests/fixtures/audit](../../tests/fixtures/audit/)，用于验证坏时间、重复值、无效功率因数和解析边界，不是业务模拟基线。

```powershell
python tools/audit_demo_inputs.py --input tests/fixtures/audit --output artifacts/audit-001
```

业务模拟台账与运行方式见 [统一模拟数据说明](../../docs/统一模拟数据说明.md)。
