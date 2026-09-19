import schemas from './generated/demo.schema.json';
import type {
  Catalog,
  Command,
  FaultRequest,
  Frame,
  JobStatus,
  Receipt,
  ResourceAvailability,
} from './generated/demo';
import { validate, type Schema } from './schema';

const base = process.env.NEXT_PUBLIC_API_BASE_URL ?? 'http://127.0.0.1:8000';
async function request<T>(
  path: string,
  schema: Schema,
  payload?: unknown,
): Promise<T> {
  const response = await fetch(`${base}${path}`, {
    signal: AbortSignal.timeout(8000),
    cache: 'no-store',
    ...(payload === undefined
      ? {}
      : {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(payload),
        }),
  });
  if (!response.ok)
    throw new Error(
      `服务请求失败 (${response.status})：${await response.text()}`,
    );
  const data: unknown = await response.json();
  validate(data, schema);
  return data as T; // justified by generated schema validation above
}
export const demoClient = {
  catalog: () => request<Catalog>('/api/demo/catalog', schemas.Catalog),
  availability: () =>
    request<ResourceAvailability>(
      '/api/demo/availability',
      schemas.ResourceAvailability,
    ),
  telemetry: () => request<Frame>('/api/demo/telemetry', schemas.Frame),
  command: (value: Command) =>
    request<Receipt>('/api/demo/commands', schemas.Receipt, value),
  fault: (value: FaultRequest) =>
    request<Frame>('/api/demo/fault', schemas.Frame, value),
  rearm: () =>
    request<{ rearmed: boolean }>(
      '/api/demo/rearm',
      {
        type: 'object',
        required: ['rearmed'],
        properties: { rearmed: { type: 'boolean' } },
        additionalProperties: false,
      },
      {},
    ),
  run: (value: unknown) =>
    request<JobStatus>('/api/simulations', schemas.JobStatus, value),
};
