import type { TimeSeriesPoint } from '../../shared/api/generated/timeseries';

export type ActivePowerPoint = {
  time: string;
  load: number | null;
  import: number | null;
  wind: number | null;
  pv: number | null;
  discharge: number | null;
  charge: number | null;
  loss: number | null;
  windArea: number | null;
  pvArea: number | null;
  dischargeArea: number | null;
  chargeBelowZero: number | null;
};

function finite(value: number | null | undefined): number | null {
  return typeof value === 'number' && Number.isFinite(value) ? value : null;
}

/** Display projection only: charging is drawn below zero; stored results stay unchanged. */
export function activePowerPoints(
  points: readonly TimeSeriesPoint[],
): ActivePowerPoint[] {
  return points.map((point) => {
    const minute = Math.round(point.time_hour * 60);
    const wind = finite(point.wind_used_mw);
    const pv = finite(point.pv_used_mw);
    const discharge = finite(point.storage_discharge_mw);
    const charge = finite(point.storage_charge_mw);
    // A partial stack would imply that an unknown source contributes zero.
    const completeSupply = wind !== null && pv !== null && discharge !== null;
    return {
      time: `${String(Math.floor(minute / 60)).padStart(2, '0')}:${String(minute % 60).padStart(2, '0')}`,
      load: finite(point.load_mw),
      import: finite(point.p_grid_optimized_mw),
      wind,
      pv,
      discharge,
      charge,
      loss: finite(point.active_loss_mw),
      windArea: completeSupply ? wind : null,
      pvArea: completeSupply ? pv : null,
      dischargeArea: completeSupply ? discharge : null,
      chargeBelowZero: charge === null ? null : charge === 0 ? 0 : -charge,
    };
  });
}

export function powerLabel(value: number | null): string {
  return value === null ? '—' : `${value.toFixed(2)} MW`;
}

export function powerDetails(point: ActivePowerPoint) {
  return [
    { label: '负荷', value: point.load },
    { label: 'PCC 受电', value: point.import },
    { label: '风电', value: point.wind },
    { label: '光伏', value: point.pv },
    { label: '储能放电', value: point.discharge },
    { label: '储能充电', value: point.charge },
    { label: '有功网损', value: point.loss },
  ];
}
