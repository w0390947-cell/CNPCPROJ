import type { SimulationResult, JobStatus } from '@/lib/simulation-api';
import type { Patent2Inputs } from '@/shared/api/generated/patent2';
import inputsSchema from '@/shared/api/generated/patent2-inputs.schema.json';
import resultSchema from '@/shared/api/generated/simulation-result.schema.json';
import liveSchema from '@/shared/api/generated/cluster-live.schema.json';
import type { ClusterLiveEvent } from '@/shared/api/generated/cluster-live';
import { validate, type Schema } from '@/shared/api/schema';

export type Scenario = 'normal' | 'low_energy';
export class HttpError extends Error {
  constructor(
    public status: number,
    message: string,
  ) {
    super(message);
  }
}
export class RecordError extends Error {}
const base = process.env.NEXT_PUBLIC_API_BASE_URL ?? 'http://127.0.0.1:8000';
async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const r = await fetch(`${base}${path}`, {
    signal: AbortSignal.timeout(30000),
    ...init,
  });
  if (!r.ok)
    throw new HttpError(
      r.status,
      `请求未完成 (${r.status})：${(await r.text()).slice(0, 400)}`,
    );
  return r.json() as Promise<T>;
}
export function parseResult(raw: unknown): SimulationResult {
  try {
    validate(raw, resultSchema as Schema);
  } catch (e) {
    throw new RecordError(
      `运行记录不符合数据契约：${e instanceof Error ? e.message : String(e)}`,
    );
  }
  const result = raw as SimulationResult;
  if (!result.patent2_evidence || !result.cluster_execution)
    throw new RecordError('该结果不是专利二实施例的完整记录。');
  const request = result.request as typeof result.request & {
    patent2_case?: Scenario;
  };
  if (
    result.metadata.scenario_type !== 'cluster_coordination' ||
    request.patent2_case !== result.patent2_evidence.inputs.scenario ||
    request.steps !== 4 ||
    result.metadata.dt_hours !== 0.25
  )
    throw new RecordError('该记录的身份或时间尺度与专利二固定实施例不一致。');
  return result;
}
export const api = {
  live: async (id: string, after: number, stream: string | null) => {
    const batch = await request<{
      stream: string | null;
      cursor: number;
      more: boolean;
      events: ClusterLiveEvent[];
    }>(
      `/api/simulations/${encodeURIComponent(id)}/live?after=${after}${stream ? `&stream=${encodeURIComponent(stream)}` : ''}`,
    );
    batch.events.forEach((event) => validate(event, liveSchema as Schema));
    return batch;
  },
  inputs: async (s: Scenario) => {
    const data = await request<Patent2Inputs>(
      `/api/patent2/inputs?scenario=${s}`,
    );
    validate(data, inputsSchema as Schema);
    return data;
  },
  library: () => request<Scenario[]>('/api/patent2/library'),
  run: (scenario: Scenario) =>
    request<JobStatus>('/api/patent2/runs', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ scenario }),
    }),
  status: (id: string) =>
    request<JobStatus>(`/api/simulations/${encodeURIComponent(id)}`),
  cancel: (id: string) =>
    request<JobStatus>(`/api/simulations/${encodeURIComponent(id)}/cancel`, {
      method: 'POST',
    }),
  result: async (id: string) =>
    parseResult(
      await request<unknown>(
        id.startsWith('library:')
          ? `/api/patent2/library/${encodeURIComponent(id.slice(8))}`
          : `/api/simulations/${encodeURIComponent(id)}/result`,
      ),
    ),
};
