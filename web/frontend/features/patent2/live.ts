import { useEffect, useRef, useState } from 'react';
import type { JobStatus } from '@/lib/simulation-api';
import type { ClusterLiveEvent } from '@/shared/api/generated/cluster-live';
import type { Patent2Inputs } from '@/shared/api/generated/patent2';
import { api } from './client';
import type { ExecutionView } from './model';

export type LiveState = {
  id: string;
  sequence: number;
  total: number;
  inputs?: Patent2Inputs;
  view?: ExecutionView;
  error: string;
};

export function appendEvent(
  state: LiveState,
  event: ClusterLiveEvent,
): LiveState {
  if (event.sequence !== state.sequence + 1)
    throw new Error('集群实时记录序号不连续');
  if (event.kind === 'cluster_study') {
    if (state.inputs || state.view) throw new Error('集群研究输入重复或迟到');
    return { ...state, sequence: event.sequence, inputs: event.inputs };
  }
  if (event.kind === 'cluster_initial') {
    if (state.view) throw new Error('集群实时初态重复');
    return {
      ...state,
      sequence: event.sequence,
      total: event.total,
      view: {
        policy: event.policy,
        safety: { domain: event.domain, steps: [] },
        rolling_updates: [],
        reserve_observations: event.observations,
        reserve_recovery_history: event.tasks,
        measurements: event.domain.regions.map((r) => ({
          region: r.region,
          time_minutes: [],
          p_mw: [],
          q_mvar: [],
          p_target_mw: [],
          q_target_mvar: [],
        })),
      },
    };
  }
  if (!state.view?.safety) throw new Error('集群实时记录缺少初态');
  const view = state.view,
    safety = view.safety!;
  if (event.kind === 'cluster_window') {
    if (event.window.start_minute > (safety.steps?.length ?? 0))
      throw new Error('规划窗口越过已完成状态');
    return {
      ...state,
      sequence: event.sequence,
      view: {
        ...view,
        rolling_updates: [
          ...(view.rolling_updates ?? []).filter(
            (w) => w.start_minute !== event.window.start_minute,
          ),
          event.window,
        ],
        reserve_observations: event.observations,
        reserve_recovery_history: event.tasks,
      },
    };
  }
  if (
    event.minute !== (safety.steps?.length ?? 0) + 1 ||
    event.step.minute + 1 !== event.minute ||
    event.minute > state.total
  )
    throw new Error('集群实时完成节点不连续');
  if (
    event.step.after.length !== safety.domain.regions.length ||
    safety.domain.regions.some(
      (r, i) =>
        event.step.after[i].p_mw.length !== r.devices.resources.length ||
        !event.pcc[r.region],
    )
  )
    throw new Error('集群实时区域或设备轴不一致');
  const previous =
    safety.steps?.at(-1)?.after ?? safety.domain.regions.map((r) => r.initial);
  if (
    event.step.before.length !== previous.length ||
    previous.some((s, i) => {
      const b = event.step.before[i];
      return (
        b.energy_mwh !== s.energy_mwh ||
        b.p_mw.length !== s.p_mw.length ||
        b.q_mvar.length !== s.q_mvar.length ||
        s.p_mw.some((p, j) => p !== b.p_mw[j]) ||
        s.q_mvar.some((q, j) => q !== b.q_mvar[j])
      );
    })
  )
    throw new Error('集群实时初末状态不衔接');
  return {
    ...state,
    sequence: event.sequence,
    view: {
      ...view,
      safety: { ...safety, steps: [...(safety.steps ?? []), event.step] },
      reserve_observations: [
        ...(view.reserve_observations ?? []),
        ...(event.step.reserve ? [event.step.reserve] : []),
      ],
      reserve_recovery_history: event.tasks,
      measurements: view.measurements.map((r) => {
        const [p, q, pt, qt] = event.pcc[r.region];
        return {
          ...r,
          time_minutes: [...(r.time_minutes ?? []), event.step.minute],
          p_mw: [...(r.p_mw ?? []), p!],
          q_mvar: [...(r.q_mvar ?? []), q!],
          // Missing economic targets stay missing; they are not replaced by backup commands.
          p_target_mw: [...(r.p_target_mw ?? []), pt],
          q_target_mvar: [...(r.q_target_mvar ?? []), qt],
        };
      }),
    },
  };
}

export function useClusterLive(job: JobStatus | null) {
  const [state, setState] = useState<LiveState | null>(null);
  const terminal = useRef(false);
  const id = job?.simulation_id;
  const jobState = job?.state;
  useEffect(() => {
    terminal.current = !!jobState && !['queued', 'running'].includes(jobState);
  }, [jobState]);
  useEffect(() => {
    if (!id) return;
    let alive = true,
      timer: ReturnType<typeof setTimeout>;
    let stream: string | null = null;
    let current: LiveState = { id, sequence: 0, total: 0, error: '' };
    async function poll() {
      try {
        const batch = await api.live(id!, current.sequence, stream);
        if (!alive) return;
        let next =
          stream === batch.stream
            ? current
            : { id: id!, sequence: 0, total: 0, error: '' };
        for (const event of batch.events) next = appendEvent(next, event);
        current = { ...next, error: '' };
        stream = batch.stream;
        setState(current);
        if (batch.more || !terminal.current)
          timer = setTimeout(poll, batch.more ? 0 : 500);
      } catch (e) {
        if (!alive) return;
        setState({
          ...current,
          error: `实时记录读取失败，保留已收到节点：${e instanceof Error ? e.message : String(e)}`,
        });
        if (!terminal.current) timer = setTimeout(poll, 1500);
      }
    }
    void poll();
    return () => {
      alive = false;
      clearTimeout(timer);
    };
  }, [id]);
  return state?.id === id ? state : null;
}
