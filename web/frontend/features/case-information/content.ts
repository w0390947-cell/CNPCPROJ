import {
  REGION_DEFINITIONS,
  type RegionCode,
} from '@/shared/lib/region-presentation';

/** Keep chapter navigation, headings and existing deep links aligned. */
export const CASE_SECTIONS = {
  identity: {
    id: 'data-identity',
    number: '01',
    title: '数据身份',
    label: '数据身份',
  },
  regions: {
    id: 'region-map',
    number: '02',
    title: '区域代码与资产尺度',
    label: '区域映射',
  },
  construction: {
    id: 'case-construction',
    number: '03',
    title: '合成算例如何构造',
    label: '算例构造',
  },
  limits: {
    id: 'study-limits',
    number: '04',
    title: '当前研究参数',
    label: '研究参数',
  },
  time: {
    id: 'time-scales',
    number: '05',
    title: '时间尺度与回放口径',
    label: '时间尺度',
  },
  boundary: {
    id: 'result-boundary',
    number: '06',
    title: '结果与校核适用边界',
    label: '结果边界',
  },
  glossary: {
    id: 'glossary',
    number: '07',
    title: '术语与符号',
    label: '术语与符号',
  },
} as const;

type RegionCaseProfile = {
  loadScaleMw: number;
  pvMw: number;
  storage: string;
  windMw: number;
};

const REGION_CASE_PROFILES: Readonly<Record<RegionCode, RegionCaseProfile>> = {
  SC: {
    loadScaleMw: 9.8,
    windMw: 10,
    pvMw: 4.2,
    storage: '2.5 MW / 5 MWh',
  },
  YA_B: {
    loadScaleMw: 8,
    windMw: 7,
    pvMw: 3.2,
    storage: '2 MW / 4 MWh',
  },
  YA_C: {
    loadScaleMw: 7.2,
    windMw: 6,
    pvMw: 3.8,
    storage: '1.8 MW / 3.6 MWh',
  },
};

export const REGION_CASE_ROWS = REGION_DEFINITIONS.map((region) => ({
  ...region,
  ...REGION_CASE_PROFILES[region.id],
}));

export const STUDY_LIMITS = [
  {
    name: '集群总受电',
    value: '≤ 26 MW',
    meaning: '三个区域每个计划时段的 PCC 受电总和上限',
  },
  {
    name: '集群等效功率因数',
    value: '≥ 0.95',
    meaning: 'ADMM 协调层的聚合有功、无功约束',
  },
  {
    name: '区域调度功率因数',
    value: '0.92',
    meaning: '区域计划采用的运行目标，不是最终验收下限',
  },
  {
    name: '独立 AC 校核功率因数',
    value: '≥ 0.90',
    meaning: '参数化算例的最终稳态校核下限',
  },
  {
    name: '母线电压',
    value: '0.95～1.05 pu',
    meaning: '非 PCC 母线的稳态电压范围',
  },
  {
    name: 'PCC 防倒送下界',
    value: '≥ 0.15 MW 或动态安全下界',
    meaning: '正值表示从上级电网受电；取更严格的逐时下界',
  },
] as const;

export const GLOSSARY = [
  ['P', '有功功率，单位通常为 MW；设备命令中正值表示向网络注入。'],
  ['Q', '无功功率，单位通常为 Mvar，用于电压和功率因数调节。'],
  ['PCC', '微网与上级电网的公共连接点，也是区域功率交换的边界。'],
  ['AC 潮流', '同时计算有功、无功、电压与线路损耗的交流稳态潮流。'],
  ['ADMM', '通过区域局部求解与集群共识迭代形成各区域 P/Q 参考。'],
  ['MISOCP', '包含离散设备状态和二阶锥约束的混合整数优化模型。'],
  ['pu', '标幺值；以选定基准值归一化后的相对量。'],
] as const;
