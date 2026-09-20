# 固定状态解析与数值回归夹具

本目录仅供独立测试，不参与业务默认数据加载。业务入口统一使用安装包的三区域台账及其 SC 导出资料。

本目录只演示文件加载与固定状态 AC 潮流，不是山城真实台账。网络为三个母线、一台变压器、一条线路；设备包括 WT1、WT2、PV1、ESS1、SVG1。所有数据在清单和文档外壳中明确标为模拟。

目标时刻：`2026-09-13T12:00:00+08:00`。安装项目后执行：

```powershell
oilfield-field run --manifest tests/fixtures/field_dataset/manifest.json --at "2026-09-13T12:00:00+08:00" --demo --output artifacts/fixture-check-001
```

正常示例 PCC 受电约 3.716104326 MW，有功网损约 0.016104326 MW。这些是自动测试的数值回归参考，不是现场实测。

修改任何文件后需更新版本并显式 `seal`；不要直接绕过摘要检查。完整格式、更新步骤与重放见 [操作说明](../../../docs3/guides/Field_Dataset_Files.md)。
