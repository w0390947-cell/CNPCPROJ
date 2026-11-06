import { useMemo } from 'react';
import {
  InputCurves,
  type InputCurvesProps,
} from '../../shared/ui/input-curves';
import { singleInputRegion } from '../../shared/lib/input-series';
import type {
  PlantInputs,
  UnifiedDomain,
} from '../../shared/api/generated/single-execution';
import type { LiveCycle } from '../../shared/api/simulation-live';

export type SingleInputPlayback = Omit<InputCurvesProps, 'regions'> & {
  rows?: LiveCycle['row'][];
};
export function SingleInputCurves({
  domain,
  total,
  forecast,
  rows,
  ...playback
}: SingleInputPlayback & {
  domain: UnifiedDomain;
  total: number;
  forecast?: PlantInputs | null;
}) {
  const regions = useMemo(
    () => [singleInputRegion(domain, total, forecast, rows)],
    [domain, total, forecast, rows],
  );
  return <InputCurves regions={regions} {...playback} />;
}
