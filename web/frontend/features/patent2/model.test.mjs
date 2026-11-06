import { test } from 'node:test';
import assert from 'node:assert/strict';
import {
  states,
  metrics,
  keyMinutes,
  recoveryTasks,
  reserveNodes,
} from './model.ts';

test('completion nodes use actual after-state; missing nodes are not interpolated', () => {
  const e = {
    safety: {
      domain: { regions: [{ initial: { energy_mwh: 2.5 } }] },
      steps: [
        { minute: 0, after: [{ energy_mwh: 2.6 }] },
        { minute: 2, after: [{ energy_mwh: 2.8 }] },
      ],
    },
  };
  assert.equal(states(e, 0)[0].energy_mwh, 2.5);
  assert.equal(states(e, 1)[0].energy_mwh, 2.6);
  assert.deepEqual(states(e, 2), []);
  assert.equal(states(e, 3)[0].energy_mwh, 2.8);
});
test('finished computation and safety replay never upgrade engineering unknown', () => {
  const e = {
    status: 'unknown',
    safety: {
      steps: [
        { source: 'economic' },
        { source: 'backup' },
        { source: 'reserve' },
      ],
    },
    rolling_updates: [{ adopted: false }, { adopted: true }],
  };
  assert.deepEqual(
    metrics({
      cluster_execution: e,
      patent2_evidence: { replayed_minutes: 3 },
    }),
    {
      completed: 3,
      economic: 1,
      backup: 1,
      adopted: 1,
      replayed: 3,
      status: 'unknown',
    },
  );
});
test('key nodes retain original deadlines and actual completion, clipped to recorded horizon', () => {
  const e = {
    safety: { steps: Array(60).fill({}) },
    rolling_updates: [0, 15, 30, 45].map((start_minute) => ({ start_minute })),
    reserve_recovery_history: [
      { first_minute: 3, deadline_minute: 18, completed_minute: 15 },
      { first_minute: 59, deadline_minute: 74 },
    ],
  };
  assert.deepEqual(keyMinutes(e), [0, 3, 15, 18, 30, 45, 59, 60]);
});
test('repeated observations close the original six tasks instead of creating new tasks', () => {
  const task = {
    region: 'SC',
    direction: 'up',
    first_minute: 3,
    deadline_minute: 18,
    last_observed_minute: 3,
    status: 'recovering',
  };
  const complete = {
    ...task,
    last_observed_minute: 15,
    completed_minute: 15,
    status: 'recovered',
  };
  const next = {
    ...task,
    first_minute: 25,
    deadline_minute: 40,
    last_observed_minute: 25,
  };
  assert.deepEqual(
    recoveryTasks({
      reserve_recovery_history: [
        task,
        complete,
        { ...task, last_observed_minute: 8 },
        next,
      ],
    }),
    [complete, next],
  );
});
test('repeated observations at the same completed node do not create extra minutes', () => {
  const first = { start_minute: 0, status: 'unknown', reason: 'initial' };
  const last = { start_minute: 0, status: 'unknown', reason: 'latest' };
  const next = { start_minute: 1, status: 'certified' };
  assert.deepEqual(
    reserveNodes({ reserve_observations: [first, last, next] }),
    [last, next],
  );
});
