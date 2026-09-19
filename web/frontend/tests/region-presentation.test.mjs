import assert from 'node:assert/strict';
import test from 'node:test';

import {
  REGION_DEFINITIONS,
  regionDisplayName,
} from '../shared/lib/region-presentation.ts';

test('region presentation names preserve internal codes and synthetic scope', () => {
  assert.deepEqual(
    REGION_DEFINITIONS.map(({ id, displayName }) => [id, displayName]),
    [
      ['SC', '山城参数化微网（SC）'],
      ['YA_B', '延安合成微网 B（YA_B）'],
      ['YA_C', '延安合成微网 C（YA_C）'],
    ],
  );
});

test('unknown region codes remain visible instead of being mislabeled', () => {
  assert.equal(regionDisplayName('SC'), '山城参数化微网（SC）');
  assert.equal(regionDisplayName('UNREGISTERED'), 'UNREGISTERED');
});
