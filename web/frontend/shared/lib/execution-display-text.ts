/** Format explanatory prose only. Never apply to stored records, IDs or exports. */
export function executionDisplayText(text: string | null | undefined): string {
  return (text ?? '')
    .replace(
      'Configurable research bounds for the seeded synthetic study; not field-validated limits',
      '可配置研究边界',
    )
    .replace(/全部网络与运行数据均为参数化模拟数据，不代表[^。]*。?/g, '')
    .replace(/软件仿真动态判据/g, '动态判据')
    .replace(/软件仿真策略/g, '策略参数')
    .replace(/软件仿真备用假设/g, '备用参数')
    .replace(/软件无功规划假设/g, '无功规划参数')
    .replace(/可配置软件研究边界/g, '可配置研究边界')
    .replace(/[；，;,]?\s*待现场(?:动态与预测误差校准|预测误差统计校准)/g, '')
    .replace(
      /[；，;,]?\s*(?:并非|非)现场(?:验收定值|运行定值|核定值|定值)/g,
      '',
    )
    .replace(/仿真观测/g, '观测')
    .replace(/模拟执行/g, '执行')
    .replace(/模拟数据集/g, '数据集')
    .replace(/模拟输入/g, '输入')
    .replace(/未模拟预测更新/g, '未进行预测更新')
    .replace(/模拟参数/g, '参数')
    .trim();
}
