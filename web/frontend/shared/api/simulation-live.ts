import type {
  UnifiedExecution,
  UnifiedStep,
  TubeState,
  SingleExecution,
} from './generated/single-execution';

export type LiveInitial = {
  input_record?: SingleExecution['input_record'];
  input_forecast?: SingleExecution['input_forecast'];
  domain?: UnifiedExecution['domain'];
  update_steps?: number;
  kind: 'initial';
  sequence: number;
  total: number;
  initial: TubeState;
  devices: UnifiedExecution['domain']['devices'];
  network: UnifiedExecution['domain']['network'];
  energy_task?: UnifiedExecution['domain']['energy_task'];
  p_band_mw?: number;
  q_band_mvar?: number;
};
export type LiveCycle = {
  kind: 'cycle';
  sequence: number;
  minute: number;
  row: Omit<UnifiedStep, 'observed' | 'cycle_evidence'> & {
    observed?: UnifiedStep['observed'];
  };
  voltage: Array<{ low: number; high: number }> | null;
  network_certified: boolean | null;
  tracking_until: number | null;
  message: string;
};
export type LivePhase = {
  kind: 'phase';
  sequence: number;
  minute: number;
  message: string;
};
export type LiveEvent = LiveInitial | LiveCycle | LivePhase;
export type LiveBatch = {
  stream: string | null;
  cursor: number;
  more: boolean;
  events: LiveEvent[];
};
