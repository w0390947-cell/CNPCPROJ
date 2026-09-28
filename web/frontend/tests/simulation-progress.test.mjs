import test from 'node:test';
import assert from 'node:assert/strict';
import { jobProgressPresentation } from '../features/simulation-progress/model.ts';

const status = (completed = 0) => ({
  state: 'running',
  stage: 'validating',
  stage_sequence: 4,
  total_stages: 6,
  rolling: {
    current_window: completed + 1,
    completed_windows: completed,
    total_windows: 96,
    start_minute: completed * 15,
    end_minute: (completed + 1) * 15,
    total_minutes: 1440,
    phase: 'preparing',
  },
});

test('rolling progress advances only with completed windows, including restored sessions', () => {
  assert.equal(jobProgressPresentation(status()).percent, 50);
  const current = status(1);
  assert.equal(jobProgressPresentation(current).percent, 50.2);
  assert.match(
    jobProgressPresentation(current).detail,
    /当前窗口 2\/96 · 仿真 00:15–00:30 · 已完成 1\/96/,
  );
  assert.deepEqual(
    jobProgressPresentation(JSON.parse(JSON.stringify(current))),
    jobProgressPresentation(current),
  );
  assert.equal(
    jobProgressPresentation({
      ...current,
      rolling: { ...current.rolling, phase: 'executing' },
    }).percent,
    50.2,
  );
});

test('window totals and final partial window are server-owned and 24:00 never wraps', () => {
  const current = status();
  current.rolling = {
    current_window: 3,
    completed_windows: 3,
    total_windows: 3,
    start_minute: 14,
    end_minute: 20,
    total_minutes: 20,
    phase: 'completed',
  };
  assert.equal(jobProgressPresentation(current).percent, 66.7);
  assert.match(
    jobProgressPresentation(current).detail,
    /00:14–00:20 · 已完成 3\/3/,
  );
  current.rolling.end_minute = 1440;
  assert.match(jobProgressPresentation(current).detail, /24:00/);
});

test('only published results reach 100 and historical nonrolling jobs still render', () => {
  for (const state of ['running', 'failed', 'cancelled', 'interrupted']) {
    assert.ok(
      jobProgressPresentation({ state, stage_sequence: 6, total_stages: 6 })
        .percent < 100,
    );
  }
  assert.ok(
    jobProgressPresentation({
      state: 'succeeded',
      result_available: false,
      stage_sequence: 6,
    }).percent < 100,
  );
  assert.equal(
    jobProgressPresentation({ state: 'succeeded', result_available: true })
      .percent,
    100,
  );
  assert.equal(jobProgressPresentation({ state: 'queued' }).percent, 0);
  assert.equal(jobProgressPresentation(null).detail, null);
  assert.equal(
    jobProgressPresentation({ state: 'running', stage_sequence: 3 }).percent,
    33.3,
  );
});

test('terminal, disconnected and stopped windows retain counts without claiming live work', () => {
  for (const state of ['failed', 'cancelled', 'interrupted']) {
    const view = jobProgressPresentation({ ...status(2), state, stage: state });
    assert.equal(view.percent, jobProgressPresentation(status(2)).percent);
    assert.match(view.detail, /最后记录窗口 3\/96.*已完成 2\/96/);
    assert.doesNotMatch(view.detail, /当前|协调与设备计划|设备执行与反馈/);
    assert.notEqual(view.stateLabel, '运行中');
  }
  assert.match(jobProgressPresentation(status(2), false).detail, /最后记录/);
  assert.equal(
    jobProgressPresentation(status(2), false).stateLabel,
    '状态待确认',
  );
  const stopped = status(2);
  stopped.rolling.phase = 'stopped';
  assert.match(jobProgressPresentation(stopped).detail, /滚动计算已中止/);
});
