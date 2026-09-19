import assert from 'node:assert/strict';
import test from 'node:test';

import {
  algorithmDefinitions,
  displayMeasurement,
} from '../features/demo-lab/presentation.ts';

test('missing demo measurements remain explicitly unknown', () => {
  assert.equal(displayMeasurement(null), '无法确认');
  assert.equal(displayMeasurement(undefined), '无法确认');
  assert.equal(displayMeasurement(1.23456), '1.235');
});

test('algorithm result links use the platform routes', () => {
  assert.deepEqual(
    Object.fromEntries(
      Object.entries(algorithmDefinitions).map(([key, value]) => [
        key,
        value.route,
      ]),
    ),
    {
      single_microgrid: '/single-microgrid',
      cluster_coordination: '/cluster-coordination',
      communication_fault: '/communication-fault',
      group_control: '/group-control',
    },
  );
});
