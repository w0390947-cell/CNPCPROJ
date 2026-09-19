# 固定版本的全系统演示资料

`bundle.json` 与 `bundle.sha256` 可直接提供给 `oilfield-demo serve/verify`。
该文件由本项目生成，全部为合成数据；13 个设备、三个区域、96 点一天曲线、12 个显式单支路故障。

```powershell
python -m oilfield_energy.entrypoints.demo serve --bundle examples/system_demo/bundle.json --output artifacts/demo-live
```

具体语义与 Web 启动步骤见[全系统演示指南](../../docs/guides/System_Simulation_Demo.md)。
重新生成等价输入可使用 `oilfield-demo generate --output <新目录>`。原数据包不被覆盖。
