import { test } from 'node:test';
import assert from 'node:assert/strict';
import {
  singleInputRegion,
  clusterInputRegions,
} from '../shared/lib/input-series.ts';

const interval = (low, high = low) => ({ low, high });
const envelope = (load, wind) => ({
  loads: [
    { bus: 'n1', p_mw: interval(load, load + 2), q_mvar: interval(-2, -1) },
  ],
  generation: [{ resource_id: 'w', minimum_available_mw: wind }],
});
const domain = {
  devices: { resources: [{ resource_id: 'w', kind: 'wind' }] },
  research: { forecast_interpolation: 'linear' },
  forecasts: [envelope(2, 1), envelope(3, 2), envelope(4, 3)],
};
const forecast = {
  step_minutes: 2,
  load_p: [{ bus_id: 'n1', values: [10, 20] }],
  load_q: [{ bus_id: 'n1', values: [-3, -5] }],
  wind_available: [{ bus_id: 'n1', values: [4, 8] }],
  pv_available: [],
};

test('single inputs keep original prediction, lower guarantee and observed intervals distinct', () => {
  const rows = [
    { step: 0, observed: envelope(8, 6) },
    { step: 2, observed: envelope(9, 7) },
  ];
  const snapshot = JSON.stringify({ domain, forecast, rows });
  const {
    metrics: [p, q, wind, pv],
  } = singleInputRegion(domain, 3, forecast, rows);
  assert.deepEqual(p.forecast, [10, 15, 20]);
  assert.deepEqual(q.forecast, [-3, -4, -5]);
  assert.deepEqual(p.declared[0], interval(2, 4));
  assert.deepEqual(p.observed, [interval(8, 10), null, interval(9, 11)]);
  assert.equal(wind.lowerOnly, true);
  assert.deepEqual(wind.forecast, [4, 6, 8]);
  assert.deepEqual(wind.declared[0], interval(1));
  assert.deepEqual(wind.observed[0], interval(6));
  assert.deepEqual(pv.forecast, [0, 0, 0]);
  assert.equal(JSON.stringify({ domain, forecast, rows }), snapshot);
});

test('old single records retain missing predictions and observations instead of using envelope midpoints', () => {
  const { metrics } = singleInputRegion(domain, 3, undefined, [{ step: 0 }]);
  assert.deepEqual(metrics[0].forecast, [null, null, null]);
  assert.deepEqual(metrics[0].observed, [null, null, null]);
  assert.deepEqual(metrics[0].declared[0], interval(2, 4));
  assert.deepEqual(
    singleInputRegion(
      { ...domain, research: { forecast_interpolation: 'hold' } },
      3,
      forecast,
    ).metrics[0].forecast,
    [10, 10, 20],
  );
});

test('cluster input regions and resource axes are joined by identity and observe interval starts', () => {
  const source = (name) => ({
    name,
    load_p_mw: { n1: [10, 20, 30, 40] },
    load_q_mvar: { n1: [1, 2, 3, 4] },
    wind_available_mw: { n1: [4, 5, 6, 7] },
    pv_available_mw: {},
  });
  const f = {
    load_p_mw: [interval(8, 12)],
    load_q_mvar: [interval(0, 2)],
    available_mw: [99, 3],
  };
  const r = (region) => ({
    region,
    devices: { resources: [{ kind: 'storage' }, { kind: 'wind' }] },
    forecasts: Array(60).fill(f),
  });
  const execution = {
    safety: {
      domain: { regions: [r('YA_B'), r('SC')] },
      steps: [{ minute: 0, observed: [f, { ...f, load_p_mw: [interval(7)] }] }],
    },
  };
  const regions = clusterInputRegions(
    { regions: [source('SC'), source('YA_B')] },
    execution,
  );
  assert.deepEqual(
    regions.map((r) => r.name),
    ['YA_B', 'SC'],
  );
  const sc = regions[1];
  assert.equal(sc.metrics[0].forecast[14], 10);
  assert.equal(sc.metrics[0].forecast[15], 20);
  assert.deepEqual(sc.metrics[0].observed[0], interval(7));
  assert.equal(sc.metrics[0].observed[1], null);
  assert.deepEqual(sc.metrics[2].declared[0], interval(3));
  assert.equal(sc.metrics[2].lowerOnly, true);
});

test('cluster preview never fabricates declared envelopes before receiving an execution domain', () => {
  const regions = clusterInputRegions({
    regions: [
      {
        name: 'SC',
        load_p_mw: { n: [1, 2, 3, 4] },
        load_q_mvar: {},
        wind_available_mw: {},
        pv_available_mw: {},
      },
    ],
  });
  assert.equal(regions[0].metrics[0].forecast[0], 1);
  assert.ok(regions[0].metrics[0].declared.every((v) => v === null));
  assert.ok(regions[0].metrics[0].observed.every((v) => v === null));
});
