import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { validate } from '../shared/api/schema.ts';
const schema = JSON.parse(
  readFileSync(
    new URL('../shared/api/generated/demo.schema.json', import.meta.url),
  ),
);

test('generated command schema rejects string numbers, missing fields and extra keys', () => {
  const command = {
    command_id: 'one',
    epoch: 'test',
    device_id: 'SC-storage',
    p_mw: 0.5,
    q_mvar: 0,
    expires_at: '2026-01-15T00:10:00Z',
  };
  validate(command, schema.Command);
  for (const bad of [
    { ...command, p_mw: '0.5' },
    { ...command, p_mw: NaN },
    { ...command, surprise: true },
    { ...command, expires_at: 'yesterday' },
  ]) {
    assert.throws(() => validate(bad, schema.Command));
  }
  assert.throws(() => validate({}, schema.Frame));
});

test('unknown safety measurements may be null, never arbitrary text', () => {
  const safety = {
    region: 'SC',
    status: 'invalid_input',
    valid: false,
    recovery_safe: false,
    reasons: ['stale'],
    pcc_import_mw: null,
  };
  validate(safety, schema.Frame.$defs.Safety, schema.Frame);
  assert.throws(() =>
    validate(
      { ...safety, pcc_import_mw: 'unknown' },
      schema.Frame.$defs.Safety,
      schema.Frame,
    ),
  );
});

test('resource availability requires a complete 96-point renewable series', () => {
  const points = Array.from({ length: 96 }, (_, index) => ({
    minute_of_day: index * 15,
    p_available_mw: 2.5,
  }));
  const availability = {
    schema_version: 'oilfield-resource-availability-v1',
    synthetic: true,
    source: 'synthetic_bundle_profile',
    dataset_id: 'test-bundle',
    period_minutes: 1440,
    series: [
      {
        device_id: 'SC-wind-1',
        region: 'SC',
        kind: 'wind',
        resolution_minutes: 15,
        points,
      },
    ],
  };
  validate(availability, schema.ResourceAvailability);
  assert.throws(() =>
    validate(
      {
        ...availability,
        series: [{ ...availability.series[0], points: points.slice(1) }],
      },
      schema.ResourceAvailability,
    ),
  );
  assert.throws(() =>
    validate(
      { ...availability, source: 'live_weather' },
      schema.ResourceAvailability,
    ),
  );
});
