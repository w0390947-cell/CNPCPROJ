# 单微网有功计划图

负责人：前端维护负责人；评审：仿真研究维护负责人。公开入口`index.ts`导出`ActivePowerChart`。

消费已完成结果的`TimeSeriesPoint[]`，以负荷/PCC折线、风电/光伏/储能放电堆叠面积和零轴下方储能充电展示有功计划。悬浮明细直接读取单项功率与后端有功网损，不显示累计堆叠值，不进行潮流、安全或经济性计算。

依赖React、Recharts、shared中由Python生成的时序契约，样式归本功能CSS Module所有；既有`panel chart-panel`类仅用于页面布局。不依赖其他功能包，不发请求、不修改归档；页面组合器负责结果身份和回放。该图保留原始时间采样点和线性连线，不创建分钟设备实绩；只用于单微网页面。

充电原始值为非负吸收功率，仅绘图投影取负；悬浮明细仍显示原始充电量并解释方向。有功与无功不混用。缺失/非有限值显示“—”，零值有效；本地供电分项缺失时整个该点正向堆叠留空，避免将未知分项当作零。风光始终使用优化采用量，不使用可用上限；网损缺失不自行推算。

验证入口：`tests/active-power-chart.test.mjs`及`tests/components/dashboard-result-identity.test.mjs`，另执行TypeScript、Oxlint和生产构建。
