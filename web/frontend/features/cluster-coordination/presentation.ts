import type { RegionalCoordinationTrace } from '@/shared/api/generated/coordination';

export type CoordinationMode = 'process' | 'plan';

export function planTimeLabel(hour: number): string {
  const minute = Math.round(hour * 60);
  return `${String(Math.floor(minute / 60)).padStart(2, '0')}:${String(minute % 60).padStart(2, '0')}`;
}

export function powerLabel(
  value: number | null | undefined,
  unit: 'MW' | 'Mvar',
): string {
  return value == null || !Number.isFinite(value)
    ? '数据缺失'
    : `${value.toFixed(2)} ${unit}`;
}

export function informationStatus(region?: RegionalCoordinationTrace): string {
  if (!region) return '未记录区域信息状态';
  const states = [];
  if (region.outage) states.push('失联');
  if (region.fallback) states.push('自治降级');
  states.push(
    region.fresh
      ? '本轮有效信息'
      : region.response_epoch == null
        ? '尚未收到申报'
        : '沿用历史申报',
  );
  return states.join(' · ');
}

export function convergenceLabel(converged: boolean | null): string {
  return converged === true
    ? '已收敛 · 最终协调参考'
    : converged === false
      ? '未收敛 · 最后一轮参考'
      : '收敛状态未记录';
}
