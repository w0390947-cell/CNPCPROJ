import assert from 'node:assert/strict';
import fs from 'node:fs';
import { createRequire } from 'node:module';
import vm from 'node:vm';
import { test } from 'node:test';
import React from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import ts from 'typescript';

const require = createRequire(import.meta.url);
const source = fs.readFileSync(
  new URL(
    '../../features/cluster-coordination/reserve-details.tsx',
    import.meta.url,
  ),
  'utf8',
);
const compiled = ts.transpileModule(source, {
  compilerOptions: { module: ts.ModuleKind.CommonJS, jsx: ts.JsxEmit.ReactJSX },
}).outputText;
const module = { exports: {} };
vm.runInNewContext(compiled, {
  exports: module.exports,
  require: (name) =>
    name.endsWith('.css')
      ? { default: {}, __esModule: true }
      : name === './presentation'
        ? { planTimeLabel: (hours) => `${hours * 60} min` }
        : require(name),
});
const { ReserveDetails } = module.exports;
const render = (execution) =>
  renderToStaticMarkup(React.createElement(ReserveDetails, { execution }));

test('historical and disabled storage results do not acquire reserve evidence', () => {
  assert.equal(render({ policy: {}, storage_enabled: true }), '');
  assert.equal(
    render({ policy: { storage_reserve: {} }, storage_enabled: false }),
    '',
  );
});

test('minute coverage counts timestamps and rejected targets retain original values and reason', () => {
  const html = render({
    storage_enabled: true,
    policy: {
      storage_reserve: {
        forecast_error_fraction: 0.02,
        minimum_error_mw: 0.05,
        support_minutes: 15,
        recovery_minutes: 15,
        trigger_fraction: 0.5,
        replan_cooldown_minutes: 5,
        source: '模拟参数',
      },
    },
    minute_reserves: [
      { minute: 7, deficient: true },
      { minute: 7, deficient: false },
    ],
    reserve_target_adjustments: [
      {
        minute: 7,
        trigger_regions: ['SC'],
        original_p_mw: { SC: [2.5] },
        original_q_mvar: { SC: [0.3] },
        update: { adopted: false, reason: '区域目标不可行' },
      },
    ],
  });
  assert.match(html, /已检查 1 个分钟时刻/);
  assert.match(html, /2\.5000 MW/);
  assert.match(html, /未采用新目标/);
  assert.match(html, /区域目标不可行/);
  assert.doesNotMatch(html, /undefined/);
});
