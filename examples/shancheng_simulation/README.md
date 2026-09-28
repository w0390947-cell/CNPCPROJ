# 山城专项模拟算例

专项配方从安装包的统一三区域台账提取 SC，不再在本目录维护另一份 recipe.json。它不代表山城真实拓扑、设备台账、现场整定或历史实绩。

配方包含十母线径向网络、风电、光伏、储能、SVG 和负荷，以及电压、功率因数、并网点功率限制、仿真时段、滚动优化窗口、求解时限和随机种子。默认生成一天 96 个 15 分钟时段的预测资料及相应分钟级模拟运行资料；具体参数以配方文件为准。

`configs/quality/` 中的配置用于开发检查工具。固定状态潮流也读取同一次生成的 SC 十母线资料，见 [固定状态入口](../shancheng_dataset/README.md)。

在已安装项目及求解依赖的环境中，从项目根目录执行：

```powershell
.\.venv\Scripts\python.exe -m oilfield_energy.entrypoints.shancheng_simulation generate `
  --output artifacts/shancheng-inputs-001

.\.venv\Scripts\python.exe -m oilfield_energy.entrypoints.shancheng_simulation run `
  --manifest artifacts/shancheng-inputs-001/manifest.json `
  --output artifacts/shancheng-run-001
```

每次运行应使用新的输出目录。生成资料、日志和结果保存在 `artifacts/`，示例目录只保留说明。从其他工作目录执行时，使用输出的绝对路径；默认输入不依赖当前目录。

全流程覆盖固定时刻 AC 潮流、固定条件扰动、日前优化、日内滚动优化、分钟执行和故障恢复检查。检查通过表示对应软件验证条件满足，不能据此认定所有故障场景安全或现场已验收。安装步骤、输出解释和适用边界见 [完整模拟指南](../../docs3/guides/Shancheng_Complete_Simulation.md)。
