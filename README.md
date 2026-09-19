# 油田源网荷储协同优化数学模型

本项目提供一套可运行的参数化模型，用于研究：

- 山城型单微电网的风—光—储—SVG协同优化；
- 三个跨区域微电网的分层集群协调；
- 凸共识ADMM下发区域P/Q目标，各区域独立AC一致MISOCP落实设备指令；
- 山城单微网设备级P/Q事务分解、联合能力校验、防重放、可调裕度和失效阻断；
- 通信延时、丢包、区域失联、自治降级和恢复重入仿真；
- 日前、日内15分钟更新与1分钟设备跟踪的多时间尺度闭环；
- 分钟级实际设备P/Q的AC网络反馈、未知/越限恢复闭锁及显式复归（参数化SIL）；
- 分布式光伏动态阈值、3/5防抖、二次限发、渐进恢复、异常恢复锁存及八类确定性回归场景；
- 群控/硬保护绝对功率上限仲裁、倒送概率有效期与保守降级、量测/ACK生产边界契约；
- PV不足后的硬保护缺口经山城控制器按“储能优先、风机其次”分解，并由显式设备适配器执行和确认；
- 项目方Excel只读质量审计、可追溯设备台账、PCC方向归一化和现场算例失效关闭；
- 风机`Q/P`、光伏无功授权及储能PCS无功授权的一致设备能力约束；
- PCC防倒送、功率因数、电压、线路容量和储能SOC约束；
- 购电成本、弃风弃光、储能损耗、网损、节点电压偏差等多目标优化；
- Branch Flow MISOCP显式有功/无功网损及独立AC逐线路复核；
- 原LinDistFlow/7切点MILP保留为可选历史对照。

所有算例数据均为模拟研究数据，不代表中国石油、长庆油田或任何实际场站。

现场资料可以通过一份清单引用独立的拓扑、设备、测点、运行方式和量测文件，执行固定时刻 AC 潮流，并保留版本、摘要与可重放归档。操作步骤见 [现场资料文件化维护说明](docs/guides/Field_Dataset_Files.md)，可运行模拟资料见 [文件集示例](examples/shancheng_dataset/README.md)。

## 快速运行

完整山城模拟数据与全算法验证可使用 `oilfield-sim generate` / `oilfield-sim run`，覆盖 96 次日内滚动优化、1,440 分钟执行及异常恢复。步骤和数据含义见 [山城完整模拟指南](docs/guides/Shancheng_Complete_Simulation.md)。

Windows PowerShell：

```powershell
& '.\.venv\Scripts\python.exe' 'run_model.py'
```

从零安装：

```powershell
& 'C:\Software\Python_3.12.1\python.exe' -m venv '.venv'
& '.\.venv\Scripts\python.exe' -m pip install -e '.'
& '.\.venv\Scripts\python.exe' 'run_model.py'
```

快速小算例：

```powershell
& '.\.venv\Scripts\python.exe' 'run_model.py' --steps 24 --output 'results\quick'
```

完整多层级控制（96时段正式结果，并附缩小版通信故障回归）：

```powershell
& '.\.venv\Scripts\python.exe' 'run_model.py' hierarchical --output 'results\hierarchical'
```

该流程已接入分钟级AC反馈，新增逐阶段电压、线路容量及有效性报告。配置、故障演示和现场边界见[分钟级网络安全反馈指南](docs/Minute_Network_Safety_Feedback.md)。

仅运行群调群控确定性场景回归（不启动MISOCP或ADMM）：

```powershell
& '.\.venv\Scripts\python.exe' 'run_model.py' group-scenarios --output 'results\group_control_scenarios'
```

只读审计项目方工作簿并生成现场数据就绪性报告：

```powershell
& '.\.venv\Scripts\python.exe' 'run_model.py' field-audit --output 'results\field_data'
```

固定其他设备状态，只改变指定设备实际P或Q，比较前后AC潮流（不重新优化）：

```powershell
& '.\.venv\Scripts\python.exe' 'run_model.py' compare-power-flow --input 'docs\examples\fixed_device_comparison.json' --output 'results\fixed_device_comparison'
```

输入、单位方向、越限报告及结果重放见[固定设备状态潮流对照指南](docs/Fixed_Device_Power_Flow_Comparison.md)。随附示例为合成数据，现场分析需提供实际网络与标准化快照。

默认`run`采用96个时段，即24小时、15分钟分辨率。一次运行依次求解：

1. 山城型单微网基准方案；
2. 山城型单微网优化方案；
3. 三微网集群基准方案；
4. 三微网集群协调优化方案。

默认求解器为开源SCIP，网络模型包含`rI²`有功损耗、`xI²`无功损耗、完整电压二阶项和Branch Flow旋转二阶锥。只有锥间隙、储能整数证书以及独立AC逐线路校核同时通过，结果才被接受。复现旧近似模型可增加`--legacy-milp`。

## 网页仿真演示

网页端为本机离线的深色调度大屏，真实调用上述 Python 模型，并覆盖单微网、集群协调、通信故障和群调群控四类场景。

先在项目根目录启动 API：

```powershell
& '.\.venv\Scripts\oilfield-web.exe'
```

再在另一个 PowerShell 窗口启动网页：

```powershell
Set-Location web\frontend
npm.cmd run dev
```

访问 `http://localhost:3000`。完整说明见 [网页仿真平台单机运行与演示指南](docs/Web_Simulation_Interface_User_Guide.md)。

## 主要输出

- `results/synthetic_case.json`：完整算例输入快照；
- `results/summary.json`：经济性、求解器状态和约束校验；
- `results/single_timeseries.csv`：单微网15分钟结果；
- `results/cluster_timeseries.csv`：三微网15分钟结果；
- `results/single_dispatch.png`：单微网调度曲线；
- `results/cluster_dispatch.png`：集群调度曲线。

多层级命令还会生成：

- `results/hierarchical/summary.json`：ADMM、分布式MISOCP、集中式基准、计划安全和群控指标；
- `results/hierarchical/admm_history.csv`：逐轮残差与降级状态；
- `results/hierarchical/regional_targets.csv`：集群P/Q目标、区域落实和集中式对照；
- `results/hierarchical/device_tracking.csv`：1分钟命令与实际响应；
- `results/hierarchical/group_control_timeseries.csv`：逐分钟风险、阈值、状态、限发恢复和本地保护；
- `results/hierarchical/group_control_events.csv`：跨区域排序的群控状态转换和动作事件；
- `results/hierarchical/network_losses.csv`：逐线路MISOCP/AC有功、无功损耗及锥间隙；
- `results/hierarchical/group_control_thresholds.png`：PCC实绩、三重阈值和控制动作；
- `results/hierarchical/pv_curtailment_and_recovery.png`：光伏计划、目标、响应、限发及恢复；
- `results/hierarchical/group_control_scenarios/`：八类群控确定性场景的摘要、时序、事件及总览图；
- `results/hierarchical/loss_model_comparison.png`：旧7切点模型与新MISOCP的网损对比；
- `results/hierarchical/fault_demo/`：延时、丢包和失联恢复回归结果。

## 测试

```powershell
& '.\.venv\Scripts\python.exe' -m unittest discover -s tests -v
```

## 文档入口

- [全系统模拟演示：三区域算法、Web、持续设备服务与异常恢复](docs/guides/System_Simulation_Demo.md)
- [山城单微网完整 24 小时模拟：固定潮流、扰动与多时间尺度执行](docs/guides/Shancheng_Complete_Simulation.md)
- [项目要求—模型—代码—测试验收追踪表](docs/Acceptance_Traceability.md)
- [项目任务书原文摘录](docs/Origin.md)
- [系统输入数据、合成算例与现场资料边界](docs/Input_Data_Guide.md)
- [设备控制层职责与工作流程说明](docs/Device_Control_Layer_Guide.md)
- [设备控制层遥测、命令与执行反馈契约](docs/Device_Control_Telemetry_Command_Contracts.md)
- [统一建模假设与指标定义](docs/Model_Assumptions.md)
- [完整数学模型](docs/Mathematical_Model.md)
- [多层级控制、通信与时间尺度说明](docs/Hierarchical_Control.md)
- [分布式光伏群调群控策略与实施状态](docs/PV_Group_Control_Strategy.md)
- [山城单微网设备级控制实现](docs/Shancheng_Control.md)
- [项目方资料接入与现场算例边界](docs/Field_Data_Integration.md)
- [现场指定时刻固定状态潮流：输入、运行和验证](docs/Field_Snapshot_Power_Flow.md)
- [代码与数据说明](docs/Implementation_Guide.md)
- [网页仿真界面详细实施方案](docs/Web_Simulation_Interface_Implementation_Plan.md)
- [网页仿真平台单机运行与演示指南](docs/Web_Simulation_Interface_User_Guide.md)
