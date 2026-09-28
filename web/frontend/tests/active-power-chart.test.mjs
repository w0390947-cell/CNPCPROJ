import assert from 'node:assert/strict';
import { test } from 'node:test';
import {
  activePowerPoints,
  powerDetails,
  powerLabel,
} from '../features/active-power-chart/presentation.ts';

// Hand-specified example: 3.1 PCC + 4 wind + 2 PV + 1 discharge = 10 load + 0.1 loss.
const source = {
  time_hour: 6.25,
  load_mw: 10,
  p_grid_optimized_mw: 3.1,
  wind_used_mw: 4,
  wind_available_mw: 8,
  pv_used_mw: 2,
  pv_available_mw: 5,
  storage_charge_mw: 0,
  storage_discharge_mw: 1,
  active_loss_mw: 0.1,
};

test('chart uses adopted per-source power and backend loss without mutating results', () => {
  const original = structuredClone(source);
  const [point] = activePowerPoints([Object.freeze(source)]);
  assert.equal(point.time, '06:15');
  assert.deepEqual(
    powerDetails(point).map((p) => p.value),
    [10, 3.1, 4, 2, 1, 0, 0.1],
  );
  assert.deepEqual(
    [point.windArea, point.pvArea, point.dischargeArea],
    [4, 2, 1],
  );
  assert.deepEqual(source, original);
});

test('charging is drawn below zero but tooltip retains the actual absorbed power', () => {
  const [point] = activePowerPoints([
    { ...source, storage_charge_mw: 2.5, storage_discharge_mw: 0 },
  ]);
  assert.equal(point.chargeBelowZero, -2.5);
  assert.equal(point.dischargeArea, 0);
  assert.equal(
    powerDetails(point).find((p) => p.label === '储能充电').value,
    2.5,
  );
  assert.equal(powerLabel(point.discharge), '0.00 MW');
});

test('missing sources break the whole stack without replacing unknown values with zero', () => {
  for (const missing of [undefined, null, NaN, Infinity]) {
    const [point] = activePowerPoints([
      { ...source, pv_used_mw: missing, active_loss_mw: missing },
    ]);
    assert.equal(point.wind, 4);
    assert.equal(point.pv, null);
    assert.equal(point.loss, null);
    assert.deepEqual(
      [point.windArea, point.pvArea, point.dischargeArea],
      [null, null, null],
    );
    assert.equal(powerLabel(point.pv), '—');
  }
});

test('disabled storage zeros, missing charging, signed PCC, and final timestamp stay distinct', () => {
  const points = activePowerPoints([
    { ...source, storage_charge_mw: 0, storage_discharge_mw: 0 },
    {
      ...source,
      time_hour: 23.75,
      storage_charge_mw: undefined,
      p_grid_optimized_mw: -0.5,
    },
  ]);
  assert.equal(points.length, 2);
  assert.equal(points[0].chargeBelowZero, 0);
  assert.equal(points[0].dischargeArea, 0);
  assert.equal(points[1].chargeBelowZero, null);
  assert.equal(points[1].time, '23:45');
  assert.equal(points[1].import, -0.5);
  assert.deepEqual(activePowerPoints([]), []);
});
