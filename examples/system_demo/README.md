# 统一数据集的设备演示

此目录不再维护独立 bundle。命令从安装包的统一台账生成捕获包，包含三个区域、14 个设备、96 点日前预测、1,440 点分钟实际值和 14 个显式单支路故障。

```powershell
oilfield-demo generate --output artifacts/demo-inputs-001
oilfield-demo serve --bundle artifacts/demo-inputs-001/bundle.json --output artifacts/demo-live
```

具体语义与 Web 启动步骤见[统一模拟数据说明](../../docs/统一模拟数据说明.md)。
重新生成等价输入可使用 `oilfield-demo generate --output <新目录>`。原数据包不被覆盖。
