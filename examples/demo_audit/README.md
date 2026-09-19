# 原始 Excel 资料审计的合成样例

三份工作簿均由确定性工程假设生成，不读取或复制甲方工作簿。仅演示导入、数据质量检查和阻断行为，不提供已批准的现场参数。

| 文件 | 内容 | 预期结果 |
|---|---|---|
| short-circuit-synthetic.xlsx | 三个模拟站的等值阻抗、短路容量和电流原值，采用旧导入器要求的第 4/6 行布局 | 读入 3 条记录；6 个容量/电流单位待确认 |
| line-load-continuous-synthetic.xlsx | 2026-01-15 的 24 个整点；P=4.5+0.7sin(hπ/12) MW，Q=0.6 Mvar，U=35 kV；I=1000√(P²+Q²)/(√3U)，PF=P/√(P²+Q²) | 无坏时间、数值或功率因数异常；5 个量测单位待确认 |
| line-load-invalid-synthetic.xlsx | 同一曲线注入坏时间、重复时间、非数值有功、零电压和 PF=1.2 | 对应问题全部检出，并检出时间间隔断裂 |

时间存储为 Excel 数值日期（1900 日期系统），格式 `yyyy-mm-dd hh:mm`，不混入时区转换。
数据页名称明确标为合成。短路数据是单独的解析样例，不与三区域潮流资料包做物理标定关联。

旧版工作簿审计器无条件生成单位待确认问题；即使这里的列名带有单位，也不会自动改成已确认。
这是要展示的严格输入边界，不能通过改造样例将其伪装成现场就绪。

执行独立审计（不会装载甲方设备台账）：

```powershell
python tools/audit_demo_inputs.py --input examples/demo_audit --output artifacts/demo-audit-report.json
```

`manifest.json` 固定当前工作簿的 SHA-256。若经审查修改样例，必须同时更新摘要及预期测试。
