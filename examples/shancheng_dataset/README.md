# 固定状态资料导出

固定状态潮流现在与山城专项研究共用统一 SC 十母线输入，不维护独立三母线业务样例。

```powershell
oilfield-sim generate --output artifacts/unified-sc-001
oilfield-field run --manifest artifacts/unified-sc-001/manifest.json --at "2026-09-15T12:00:00+08:00" --demo --output artifacts/unified-fixed-001
```

原三母线样例保留在 `tests/fixtures/field_dataset/`，仅供独立导入与数值回归。详情见 [统一模拟数据说明](../../docs/统一模拟数据说明.md)。
