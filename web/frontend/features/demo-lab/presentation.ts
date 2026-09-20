export const algorithmDefinitions: Record<
  string,
  { label: string; route: string }
> = {
  single_microgrid: { label: '单微网优化', route: '/single-microgrid' },
  cluster_coordination: { label: '三区域协同', route: '/cluster-coordination' },
  communication_fault: { label: '通信故障', route: '/communication-fault' },
  group_control: { label: '光伏群控策略验证', route: '/group-control' },
};

export const deviceKindLabels = {
  wind: '风电',
  pv: '光伏',
  storage: '储能',
  svg: 'SVG',
} as const;

export const displayMeasurement = (
  value: number | null | undefined,
  digits = 3,
) => (value == null ? '无法确认' : value.toFixed(digits));
