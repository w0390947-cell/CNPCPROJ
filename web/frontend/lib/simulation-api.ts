import type { ReferenceEconomics } from '../shared/api/generated/economics';
import type { ComputationQualityReport } from '../shared/api/generated/computation-quality';
import type { JobStatus } from '../shared/api/generated/job-status';
export type { JobState, JobStatus } from '../shared/api/generated/job-status';
import type { ClusterExecution } from '../shared/api/generated/cluster-execution';
import type {
  ClusterValidation,
  CoordinationSnapshot,
} from '../shared/api/generated/cluster';
import type {
  ADMMHistoryPoint,
  ClusterTimeSeriesPoint,
} from '../shared/api/generated/coordination';
import type {
  CommunicationEventExecution,
  ScenarioEvent,
} from '../shared/api/generated/study-events';
export type { ScenarioEvent } from '../shared/api/generated/study-events';

export type { TimeSeriesPoint as SimulationPoint } from '../shared/api/generated/timeseries';
import type { TimeSeriesPoint as SimulationPoint } from '../shared/api/generated/timeseries';

export type SimulationResult = {
  computation_quality?: ComputationQualityReport | null;
  economic_accounting_version?: string | null;
  reference_economics?: ReferenceEconomics | null;
  cluster_validation?: ClusterValidation | null;
  coordination_snapshot?: CoordinationSnapshot | null;
  cluster_execution?: ClusterExecution | null;
  metadata: {
    dataset_id?: string | null;
    dataset_revision?: string | null;
    dataset_sha256?: string | null;
    profile_kind?: string | null;
    schema_version: string;
    scenario_type: ScenarioType;
    region: string;
    steps: number;
    formulation: string;
    generated_at: string;
    data_notice: string;
    dt_hours: number;
  };
  request: {
    region: SimulationConfiguration['region'];
    steps: number;
    storage_enabled: boolean;
    solver: {
      time_limit_seconds: number;
      quality_policy?: 'quality-first-v1' | null;
    };
    admm_max_iterations: number;
    communication_loss_probability: number;
    communication_max_delay_iterations: number;
    communication_outage_region: SimulationConfiguration['region'];
    communication_outage_start_iteration: number;
    communication_outage_end_iteration: number;
    events: ScenarioEvent[];
  };
  executive_summary: {
    overall_passed: boolean;
    headline: string;
    economic_improvement_percent: number;
    optimized_economic_cost_cny: number;
    optimized_peak_import_mw: number;
    total_active_loss_mwh: number;
  };
  topology_nodes: Array<{ id: string; label: string; kind: string }>;
  topology_edges: Array<{ id: string; source: string; target: string }>;
  validation_items: Array<{
    code: string;
    label: string;
    passed: boolean;
    explanation: string;
    actual?: number | string | boolean | null;
    limit?: number | string | boolean | null;
    scope?: string;
    validation_basis?:
      | 'centralized_reference'
      | 'admm_reference'
      | 'day_ahead'
      | 'intraday'
      | 'minute'
      | null;
    assessment_status?:
      | 'passed'
      | 'violated'
      | 'unknown'
      | 'not_computed'
      | null;
  }>;
  timeseries: SimulationPoint[];
  cluster_timeseries: ClusterTimeSeriesPoint[];
  admm_history: ADMMHistoryPoint[];
  communication: {
    event_executions?: CommunicationEventExecution[];
    sent: number;
    delivered: number;
    dropped: number;
    delayed: number;
    outage_dropped: number;
    stale_uses: number;
    fallback_uses: number;
    loss_probability: number;
    loss_start_iteration: number | null;
    loss_end_iteration: number | null;
    outage_region: string | null;
    outage_start_iteration: number | null;
    outage_end_iteration: number | null;
  } | null;
  group_control: {
    all_passed: boolean;
    scenario_count: number;
    scenario_checks: Record<string, Record<string, boolean>>;
    records: Array<{
      scenario: string;
      time_minute: number;
      pcc_power_mw: number;
      risk_limit_mw: number;
      safety_threshold_mw: number;
      restore_threshold_mw: number;
      risk_index: number;
      state: string;
      action: string;
      trigger_reason: string;
      requested_curtailment_mw: number;
      requested_restoration_mw: number;
      remaining_curtailment_mw: number;
    }>;
    events: Array<{
      scenario: string;
      time_minute: number;
      event_type: string;
      reason: string;
      previous_state: string;
      new_state: string;
      requested_power_mw: number;
      achieved_power_mw: number;
    }>;
  } | null;
};

export type ScenarioType =
  | 'single_microgrid'
  | 'cluster_coordination'
  | 'communication_fault'
  | 'group_control';

export type SimulationConfiguration = {
  region: 'SC' | 'YA_B' | 'YA_C';
  steps: number;
  storageEnabled: boolean;
  timeLimitSeconds: number;
  admmMaxIterations: number;
  communicationLossProbability: number;
  communicationMaxDelayIterations: number;
  communicationOutageRegion: 'SC' | 'YA_B' | 'YA_C';
  communicationOutageStartIteration: number;
  communicationOutageEndIteration: number;
};

export class SimulationCancelledError extends Error {
  constructor() {
    super('仿真已取消');
    this.name = 'SimulationCancelledError';
  }
}

const API_BASE =
  process.env.NEXT_PUBLIC_API_BASE_URL ?? 'http://127.0.0.1:8000';

export type HealthResponse = {
  status: string;
  service: string;
  schema_version: string;
  solvers: string[];
  packages: Record<string, string>;
};

function websocketUrl(simulationId: string): string {
  const url = new URL(`${API_BASE}/api/simulations/${simulationId}/stream`);
  url.protocol = url.protocol === 'https:' ? 'wss:' : 'ws:';
  return url.toString();
}

async function apiRequest<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`${API_BASE}${path}`, {
    signal: AbortSignal.timeout(15000),
    ...init,
  });
  if (!response.ok) {
    const raw = await response.text();
    let detail = raw || response.statusText;
    try {
      const body = JSON.parse(raw) as { detail?: unknown };
      if (body.detail != null)
        detail =
          typeof body.detail === 'string'
            ? body.detail
            : JSON.stringify(body.detail);
    } catch {
      /* 非JSON错误也保留正文；响应流只读取一次。 */
    }
    throw new Error(`仿真服务返回 ${response.status}: ${detail}`);
  }
  return response.json() as Promise<T>;
}

export function getHealth(): Promise<HealthResponse> {
  return apiRequest<HealthResponse>('/api/health');
}

/** Backend-managed quality for optimization views; research scenarios stay manual. */
export function usesQualityPolicy(scenarioType: ScenarioType): boolean {
  return (
    scenarioType === 'cluster_coordination' ||
    scenarioType === 'single_microgrid'
  );
}

/** Only cluster tasks override the configured time grid. */
export function effectiveSimulationSteps(
  scenarioType: ScenarioType,
  configuredSteps: number,
): number {
  return scenarioType === 'cluster_coordination' ? 96 : configuredSteps;
}

function simulationPayload(
  scenarioType: ScenarioType,
  configuration: SimulationConfiguration,
  events: ScenarioEvent[],
) {
  const steps = effectiveSimulationSteps(scenarioType, configuration.steps);
  return {
    name: `${configuration.region} ${steps}点${scenarioType}网页仿真`,
    scenario_type: scenarioType,
    region: configuration.region,
    steps,
    storage_enabled: configuration.storageEnabled,
    events,
    ...(usesQualityPolicy(scenarioType)
      ? {}
      : { admm_max_iterations: configuration.admmMaxIterations }),
    communication_loss_probability: configuration.communicationLossProbability,
    communication_max_delay_iterations:
      configuration.communicationMaxDelayIterations,
    communication_outage_region: configuration.communicationOutageRegion,
    communication_outage_start_iteration:
      configuration.communicationOutageStartIteration,
    communication_outage_end_iteration:
      configuration.communicationOutageEndIteration,
    solver: {
      formulation: 'misocp',
      ...(usesQualityPolicy(scenarioType)
        ? { quality_policy: 'quality-first-v1' }
        : { time_limit_seconds: configuration.timeLimitSeconds }),
    },
  };
}

export function createSimulation(
  scenarioType: ScenarioType = 'single_microgrid',
  configuration: SimulationConfiguration,
  events: ScenarioEvent[] = [],
): Promise<JobStatus> {
  return apiRequest<JobStatus>('/api/simulations', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(
      simulationPayload(scenarioType, configuration, events),
    ),
  });
}

export function getSimulation(simulationId: string): Promise<JobStatus> {
  return apiRequest<JobStatus>(`/api/simulations/${simulationId}`);
}

export function cancelSimulation(simulationId: string): Promise<JobStatus> {
  return apiRequest<JobStatus>(`/api/simulations/${simulationId}/cancel`, {
    method: 'POST',
  });
}

export function getSimulationResult(
  simulationId: string,
): Promise<SimulationResult> {
  return apiRequest<SimulationResult>(
    `/api/simulations/${simulationId}/result`,
  );
}

export async function waitForSimulation(
  initial: JobStatus,
  onStatus: (status: JobStatus) => void,
  pollingIntervalMs = 350,
  signal?: AbortSignal,
): Promise<SimulationResult> {
  let status = initial;
  onStatus(status);

  signal?.throwIfAborted();
  try {
    if (!['queued', 'running'].includes(status.state))
      throw new Error('Already terminal');
    status = await new Promise<JobStatus>((resolve, reject) => {
      const socket = new WebSocket(websocketUrl(initial.simulation_id));
      let settled = false;
      let watchdog: ReturnType<typeof setTimeout>;
      const cleanup = () => {
        clearTimeout(watchdog);
        signal?.removeEventListener('abort', abort);
        socket.close();
      };
      const fail = (reason = 'WebSocket status stream unavailable') => {
        if (settled) return;
        settled = true;
        cleanup();
        reject(new Error(reason));
      };
      const abort = () => fail('Subscription aborted');
      const armWatchdog = () => {
        clearTimeout(watchdog);
        watchdog = setTimeout(() => fail('Status stream timed out'), 10000);
      };
      signal?.addEventListener('abort', abort, { once: true });
      armWatchdog();
      socket.onmessage = (event) => {
        let update: JobStatus;
        try {
          update = JSON.parse(String(event.data)) as JobStatus;
        } catch {
          fail('Invalid status message');
          return;
        }
        if (
          update.simulation_id !== initial.simulation_id ||
          ![
            'queued',
            'running',
            'succeeded',
            'failed',
            'cancelled',
            'interrupted',
          ].includes(update.state)
        ) {
          fail('Mismatched status message');
          return;
        }
        status = update;
        armWatchdog();
        onStatus(update);
        if (!['queued', 'running'].includes(update.state)) {
          settled = true;
          resolve(update);
          cleanup();
        }
      };
      socket.onerror = () => fail();
      socket.onclose = () => {
        if (!settled) fail();
      };
    });
  } catch {
    signal?.throwIfAborted();
    while (status.state === 'queued' || status.state === 'running') {
      await new Promise((resolve) =>
        globalThis.setTimeout(resolve, pollingIntervalMs),
      );
      signal?.throwIfAborted();
      status = await getSimulation(status.simulation_id);
      signal?.throwIfAborted();
      onStatus(status);
    }
  }

  signal?.throwIfAborted();

  if (status.state === 'succeeded') {
    return getSimulationResult(status.simulation_id);
  }
  if (status.state === 'cancelled') {
    throw new SimulationCancelledError();
  }
  throw new Error(
    status.error_message ?? `仿真任务以 ${status.state} 状态结束`,
  );
}
