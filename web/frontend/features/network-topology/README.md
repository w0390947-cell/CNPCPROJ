# 网络拓扑

保留单微网优化原有的 PCC、主母线、风电、光伏、储能／SVG 五组卡片、图标与连线布局，展示当前回放帧的区域汇总计划功率。五组表示功能分组，不表示模型只有五条母线，也不表示单台设备实测值。

公开入口为 `index.ts`；接收 `current: ProfileFrame` 和结果所属 `region`，不读取网络台账或重新计算物理模型。复用 `lib/simulation-presentation.ts` 的既有回放类型与数值格式、`shared/lib/region-presentation.ts` 的区域名称和 `lucide-react` 图标。沿用 `app/globals.css` 中原有 `single-network`、`single-device`、`single-link` 样式，保持原界面外观。

统一山城十母线、YA_B／YA_C 五母线及历史结果均通过既有结果回放投影展示；风电与光伏分别使用区域汇总采用出力，储能正值为放电，SVG 正值为容性注入。未运行或无有效值时保持卡片，以破折号表示缺少数据。

负责人：前端维护负责人；验证：组件测试与 TypeScript 检查。

风电计划同时显示区域汇总有功P（MW）和无功Q（Mvar），Q直接来自优化结果`wind_q_mvar`，正值为注入、负值为吸收；不使用比例上限估算计划。P/Q使用同一结果、时间轴和既有分钟插值规则；任一端点缺少Q时不插值补造，历史缺失显示“—”，有效零值显示“0.00”。局部排版由`power-plan.module.css`维护；传输类型由Python契约生成至`shared/api/generated/timeseries.ts`。
