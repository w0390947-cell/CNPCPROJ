import type {
  PlantInputs,
  UnifiedDomain,
  UnifiedStep,
} from '../api/generated/single-execution';
import type { Patent2Inputs } from '../api/generated/patent2';
import type { ExecutionView } from '../../features/patent2/model';

export type InputRange = { low: number; high: number };
export type InputMetric = {
  key: string;
  title: string;
  unit: string;
  lowerOnly: boolean;
  forecast: (number | null)[];
  declared: (InputRange | null)[];
  observed: (InputRange | null)[];
};
export type InputRegion = {
  name: string;
  total: number;
  metrics: InputMetric[];
};
type Observation = Pick<UnifiedStep, 'step'> & {
  observed?: UnifiedStep['observed'];
};
const specs = [
  { key: 'p', title: '负荷有功 P', unit: 'MW', lowerOnly: false },
  { key: 'q', title: '负荷无功 Q', unit: 'Mvar', lowerOnly: false },
  { key: 'wind', title: '风电可用功率', unit: 'MW', lowerOnly: true },
  { key: 'pv', title: '光伏可用功率', unit: 'MW', lowerOnly: true },
] as const;
const point = (v: number): InputRange => ({ low: v, high: v });
function sum(values: number[]): number | null {
  return values.every(Number.isFinite)
    ? values.reduce((a, b) => a + b, 0)
    : null;
}
function range(values: InputRange[]): InputRange | null {
  const low = sum(values.map((v) => v.low)),
    high = sum(values.map((v) => v.high));
  return low == null || high == null ? null : { low, high };
}

/** Preserve missing predictions; envelope midpoints are not prediction evidence. */
export function singleInputRegion(
  domain: UnifiedDomain,
  total: number,
  forecast?: PlantInputs | null,
  rows: Observation[] = [],
): InputRegion {
  const observed = new Map(rows.map((r) => [r.step, r.observed]));
  const resources = domain.devices.resources;
  return {
    name: 'SC',
    total,
    metrics: specs.map((spec) => {
      const get = (
        f?: UnifiedDomain['forecasts'][number],
      ): InputRange | null => {
        if (!f) return null;
        if (spec.key === 'p' || spec.key === 'q')
          return range(
            f.loads.map((l) => (spec.key === 'p' ? l.p_mw : l.q_mvar)),
          );
        const values = resources
          .filter((r) => r.kind === spec.key)
          .map(
            (r) =>
              f.generation.find((g) => g.resource_id === r.resource_id)
                ?.minimum_available_mw ?? NaN,
          );
        const value = sum(values);
        return value == null ? null : point(value);
      };
      const source =
        forecast?.[
          spec.key === 'p'
            ? 'load_p'
            : spec.key === 'q'
              ? 'load_q'
              : spec.key === 'wind'
                ? 'wind_available'
                : 'pv_available'
        ];
      return {
        ...spec,
        forecast: Array.from({ length: total }, (_, step) => {
          if (!source || !forecast) return null;
          return sum(
            source.map((s) => {
              const k = Math.min(
                Math.floor(step / forecast.step_minutes),
                s.values.length - 1,
              );
              const fraction =
                domain.research.forecast_interpolation === 'linear'
                  ? (step % forecast.step_minutes) / forecast.step_minutes
                  : 0;
              return (
                (1 - fraction) * s.values[k] +
                fraction * s.values[Math.min(k + 1, s.values.length - 1)]
              );
            }),
          );
        }),
        declared: Array.from({ length: total }, (_, i) =>
          get(domain.forecasts[i]),
        ),
        observed: Array.from({ length: total }, (_, i) => get(observed.get(i))),
      };
    }),
  };
}

export function clusterInputRegions(
  inputs?: Patent2Inputs | null,
  execution?: ExecutionView,
): InputRegion[] {
  const regions = execution?.safety?.domain.regions;
  const names =
    regions?.map((r) => r.region) ?? inputs?.regions.map((r) => r.name) ?? [];
  return names.map((name) => {
    const domain = regions?.find((r) => r.region === name);
    const raw = inputs?.regions.find((r) => r.name === name);
    const total = domain?.forecasts.length || 60;
    const index = regions?.findIndex((r) => r.region === name) ?? -1;
    const observed = new Map(
      (execution?.safety?.steps ?? []).map((s) => [
        s.minute,
        s.observed?.[index],
      ]),
    );
    return {
      name,
      total,
      metrics: specs.map((spec) => {
        const get = (
          f?: NonNullable<typeof domain>['forecasts'][number],
        ): InputRange | null => {
          if (!f || !domain) return null;
          if (spec.key === 'p' || spec.key === 'q')
            return range(spec.key === 'p' ? f.load_p_mw : f.load_q_mvar);
          const values = domain.devices.resources.flatMap((r, i) =>
            r.kind === spec.key ? [f.available_mw[i]] : [],
          );
          const value = sum(values);
          return value == null ? null : point(value);
        };
        const source =
          raw?.[
            spec.key === 'p'
              ? 'load_p_mw'
              : spec.key === 'q'
                ? 'load_q_mvar'
                : spec.key === 'wind'
                  ? 'wind_available_mw'
                  : 'pv_available_mw'
          ];
        return {
          ...spec,
          // The fixed cluster example declares four 15-minute forecast periods.
          forecast: Array.from({ length: total }, (_, i) =>
            source
              ? sum(Object.values(source).map((v) => v[Math.floor(i / 15)]))
              : null,
          ),
          declared: Array.from({ length: total }, (_, i) =>
            get(domain?.forecasts[i]),
          ),
          observed: Array.from({ length: total }, (_, i) =>
            get(observed.get(i)),
          ),
        };
      }),
    };
  });
}
