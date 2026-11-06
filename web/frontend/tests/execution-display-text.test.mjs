import { test } from 'node:test';
import assert from 'node:assert/strict';
import { executionDisplayText } from '../shared/lib/execution-display-text.ts';

test('source descriptions retain parameters while omitting field comparison clauses', () => {
  const cases = [
    [
      '软件仿真策略：工程限值、数值容差与分钟动态规则随结果记录；非现场验收定值',
      '策略参数：工程限值、数值容差与分钟动态规则随结果记录',
    ],
    [
      '可配置软件研究边界：负荷±2.5%，风电/光伏可用下界扣除额定5%/15%；非现场定值',
      '可配置研究边界：负荷±2.5%，风电/光伏可用下界扣除额定5%/15%',
    ],
    [
      '软件仿真备用假设（参数随结果记录）；待现场预测误差统计校准，非现场运行定值',
      '备用参数（参数随结果记录）',
    ],
    [
      '软件无功规划假设（参数随结果记录）；待现场动态与预测误差校准',
      '无功规划参数（参数随结果记录）',
    ],
    ['研究要求：一分钟内响应；并非现场核定值', '研究要求：一分钟内响应'],
    [
      'Configurable research bounds for the seeded synthetic study; not field-validated limits',
      '可配置研究边界',
    ],
    [
      '同版本模拟数据集：日前预测、日内更新与原生一分钟模拟输入；同一事件窗口',
      '同版本数据集：日前预测、日内更新与原生一分钟输入；同一事件窗口',
    ],
  ];
  for (const [source, expected] of cases)
    assert.equal(executionDisplayText(source), expected);
});

test('formatting does not rewrite stored provenance or technical conclusions', () => {
  const record = {
    source: '软件仿真动态判据：最长等待10分钟；非现场验收定值',
    status: 'unknown',
  };
  const original = JSON.stringify(record);
  assert.equal(executionDisplayText(record.source), '动态判据：最长等待10分钟');
  assert.equal(JSON.stringify(record), original);
  for (const text of [
    '预测未记录',
    '备用暂未获证明',
    '输入超出声明范围',
    '周期末跟踪不代表连续跟踪通过',
    '仿真服务已连接',
    'simulation-id:synthetic_execution',
  ]) {
    assert.equal(executionDisplayText(text), text);
  }
  assert.equal(executionDisplayText(undefined), '');
});
