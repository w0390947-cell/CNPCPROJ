import test from 'node:test';
import assert from 'node:assert/strict';
import {
  clockLabel, emptyFrame, groupScenarioLabels, minuteFrames, numberLabel,
  outageAtIteration, profileFrames, resultConfiguration,
} from '../lib/simulation-presentation.ts';

const sample = (hour, wind = 4, pv = 2) => ({
  time_hour: hour, load_mw: 9, p_grid_optimized_mw: 3,
  wind_used_mw: wind, pv_used_mw: pv, storage_charge_mw: 1,
  storage_discharge_mw: 0, svg_q_mvar: 1.5,
});
const result = { timeseries: [sample(0), sample(.25, 2, 3)], cluster_timeseries: [] };

test('no result means no invented numbers, curve, or passing verdict', () => {
  assert.deepEqual(profileFrames(null), []);
  for (const value of [null, undefined, NaN, Infinity, -Infinity]) assert.equal(numberLabel(value), '—');
  assert.equal(numberLabel(0), '0.00');
  assert.equal(numberLabel(-1.2), '-1.20');
});

test('quarter-hour and final-point labels preserve actual timestamps', () => {
  assert.equal(clockLabel(15), '00:15');
  assert.equal(clockLabel(1425), '23:45');
  assert.deepEqual(profileFrames(result).map(p => p.time), ['00:00', '00:15']);
});

test('wind and PV remain separate; storage discharge and capacitive Q are positive', () => {
  const [frame] = profileFrames(result);
  assert.equal(frame.wind, 4); assert.equal(frame.pv, 2);
  assert.equal(frame.renewable, 6); assert.equal(frame.storage, -1); assert.equal(frame.svg, 1.5);
});

test('wind Q preserves signed plans, interpolates with P, and never fills missing history', () => {
  const result = { cluster_timeseries: [], timeseries: [
    { ...sample(0), wind_q_mvar: -0.6 },
    { ...sample(2 / 60), wind_q_mvar: 0.6 },
  ] };
  assert.deepEqual(minuteFrames(profileFrames(result, true)).map(p => p.windQ), [-0.6, 0, 0.6]);
  assert.equal(numberLabel(profileFrames({ ...result, timeseries: [
    { ...sample(0), wind_q_mvar: 0 },
  ] }, true)[0].windQ), '0.00');
  for (const missing of [undefined, null]) {
    result.timeseries[0].wind_q_mvar = missing;
    const frames = minuteFrames(profileFrames(result, true));
    assert.deepEqual(frames.map(p => p.windQ), [null, null, 0.6]);
    assert.equal(numberLabel(frames[0].windQ), '—');
  }
});

test('minute interpolation stops at last measured/planned point without next-day wrap', () => {
  const frames = minuteFrames(profileFrames(result));
  assert.equal(frames.length, 16);
  assert.equal(frames.at(-1).time, '00:15');
  assert.equal(frames[6].pv, 2.4);
  assert.equal(frames.at(-1).wind, 2);
});

test('interpolation cannot manufacture a missing device value or mutate inputs', () => {
  const points = [{ ...emptyFrame, time: '00:00' }, { ...emptyFrame, time: '00:02', minute: 2 }];
  const before = structuredClone(points);
  assert.equal(minuteFrames(points)[1].pv, null);
  assert.deepEqual(points, before);
  assert.deepEqual(minuteFrames([]), []);
  assert.deepEqual(minuteFrames([points[0]]), [points[0]]);
  assert.throws(() => minuteFrames([points[1], points[0]]), /严格递增/);
});

test('cluster power does not acquire made-up per-device values', () => {
  const combined = { ...result, cluster_timeseries: [{ time_hour: .5, aggregate_import_mw: 8,
    aggregate_load_mw: 20, aggregate_renewable_mw: 12 }] };
  assert.equal(profileFrames(combined)[0].import, 8);
  assert.equal(profileFrames(combined)[0].pv, null);
  assert.equal(profileFrames(combined, true)[0].pv, 2);
});

test('outage replay matches backend inclusive iteration interval', () => {
  const r = { communication: { outage_start_iteration: 12, outage_end_iteration: 22 } };
  for (const i of [0, 11, 23, 100]) assert.equal(outageAtIteration(r, i), false);
  for (const i of [12, 15, 22]) assert.equal(outageAtIteration(r, i), true);
  assert.equal(outageAtIteration(null, 12), false);
});

test('multiple outage replay uses actual per-region coverage and preserves unreached windows', () => {
  const r = { communication: { outage_start_iteration: null, outage_end_iteration: null,
    event_executions: [
      { window: { kind: 'outage', target: 'SC' }, observed_ticks: [3, 4, 5] },
      { window: { kind: 'outage', target: 'YA_B' }, observed_ticks: [5, 6, 7] },
      { window: { kind: 'outage', target: 'YA_C' }, observed_ticks: [] },
    ] } };
  assert.equal(outageAtIteration(r, 5, 'SC'), true);
  assert.equal(outageAtIteration(r, 5, 'YA_B'), true);
  assert.equal(outageAtIteration(r, 5, 'YA_C'), false);
  assert.equal(outageAtIteration(r, 6, 'SC'), false);
  assert.equal(outageAtIteration(r, 7), true);
  assert.equal(outageAtIteration(r, 8), false);
});

test('explicit global loss does not hide a separately configured default outage', () => {
  const r = { communication: { outage_start_iteration: 12, outage_end_iteration: 22,
    outage_region: 'SC', event_executions: [{ window: { kind: 'packet_loss' }, observed_ticks: [4] }] } };
  assert.equal(outageAtIteration(r, 14, 'SC'), true);
  assert.equal(outageAtIteration(r, 14, 'YA_B'), false);
});

test('all eight backend scenarios have readable labels', () => {
  for (const name of ['normal_operation', 'emergency_curtailment', 'consecutive_three_trigger',
    'three_in_five_debounce', 'secondary_curtailment', 'successful_progressive_recovery',
    'recovery_response_failure', 'communication_failure_and_reentry']) assert.ok(groupScenarioLabels[name]);
});

test('event branch configuration comes from the saved request, not UI defaults', () => {
  const r = { request: { region: 'YA_C', steps: 96, storage_enabled: false,
    solver: { time_limit_seconds: 180 }, admm_max_iterations: 450,
    communication_loss_probability: .15, communication_max_delay_iterations: 6,
    communication_outage_region: 'SC', communication_outage_start_iteration: 40,
    communication_outage_end_iteration: 50 } };
  const config = resultConfiguration(r);
  assert.equal(config.region, 'YA_C'); assert.equal(config.steps, 96);
  assert.equal(config.storageEnabled, false); assert.equal(config.communicationOutageRegion, 'SC');
  assert.equal(config.admmMaxIterations, 450); assert.equal(config.timeLimitSeconds, 180);
  assert.equal(config.communicationOutageEndIteration, 50);
});
