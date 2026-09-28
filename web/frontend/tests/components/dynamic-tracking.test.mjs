import assert from 'node:assert/strict';
import fs from 'node:fs';
import { createRequire } from 'node:module';
import vm from 'node:vm';
import { test } from 'node:test';
import React from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import { JSDOM } from 'jsdom';
import ts from 'typescript';

const require = createRequire(import.meta.url);
const source = fs.readFileSync(
  new URL(
    '../../features/cluster-coordination/dynamic-tracking.tsx',
    import.meta.url,
  ),
  'utf8',
);
const compiled = ts.transpileModule(source, {
  compilerOptions: { module: ts.ModuleKind.CommonJS, jsx: ts.JsxEmit.ReactJSX },
}).outputText;
const compiledModule = { exports: {} };
vm.runInNewContext(compiled, {
  exports: compiledModule.exports,
  require: (name) =>
    name.endsWith('.css') ? { default: {}, __esModule: true } : require(name),
});
const { DynamicTrackingDetails } = compiledModule.exports;
const assessment = {
  unit: 'Mvar',
  limit: 0.05,
  numerical_tolerance: 1e-6,
  status: 'passed',
  response_status: 'passed',
  steady_status: 'passed',
  raw_max_error: 0.60653,
  raw_max_error_time_minute: 60,
  rmse: 0.18,
  post_deadline_max_error: 0.04978,
  longest_outside_band_minutes: 5,
  invalid_samples: 0,
  transitions: [
    {
      start_minute: 60,
      allowed_response_minutes: 6,
      entered_band_after_minutes: 6,
      confirmed_after_minutes: 8,
      post_deadline_max_error: 0.04978,
      reason: '响应期限内进入偏差带，期限后持续满足偏差要求',
    },
  ],
};
function render(regions) {
  return new JSDOM(
    renderToStaticMarkup(
      React.createElement(DynamicTrackingDetails, {
        regions,
        policy: { maximum_response_minutes: 10, confirmation_samples: 3 },
      }),
    ),
  ).window.document;
}

test('large transient peak is retained alongside the backend response verdict', () => {
  const document = render([
    { region: 'YA_B', dynamic_tracking: { q: assessment } },
  ]);
  assert.match(document.body.textContent, /0\.607 Mvar \/ 01:00/);
  assert.match(document.querySelector('tbody tr').textContent, /通过 \/ 通过/);
  assert.match(document.body.textContent, /6\.000 分钟/);
  assert.match(document.body.textContent, /8\.000 分钟/);
  assert.match(document.body.textContent, /最长 10 分钟/);
});

test('insufficient observations and real failures retain separate backend states', () => {
  const document = render([
    {
      region: 'SC',
      dynamic_tracking: {
        q: {
          ...assessment,
          status: 'violated',
          response_status: 'unknown',
          steady_status: 'violated',
          post_deadline_max_error: null,
          transitions: [],
        },
      },
    },
  ]);
  assert.match(
    document.querySelector('tbody tr').textContent,
    /无法确认 \/ 未通过/,
  );
  assert.match(document.querySelector('tbody tr').textContent, /无足够证据/);
});

test('historical records without dynamic evidence do not acquire new certificates', () => {
  assert.equal(render([{ region: 'SC' }]).body.textContent, '');
});
