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
  new URL('../../shared/ui/input-curves.tsx', import.meta.url),
  'utf8',
);
const loaded = { exports: {} };
vm.runInNewContext(
  ts.transpileModule(source, {
    compilerOptions: {
      module: ts.ModuleKind.CommonJS,
      jsx: ts.JsxEmit.ReactJSX,
    },
  }).outputText,
  {
    exports: loaded.exports,
    require: (name) =>
      name.endsWith('.css')
        ? { default: new Proxy({}, { get: (_, key) => key }), __esModule: true }
        : require(name),
  },
);
const { InputCurves } = loaded.exports;
const range = (low, high = low) => ({ low, high });
const metric = (key, lowerOnly = false) => ({
  key,
  title: key,
  unit: 'MW',
  lowerOnly,
  forecast: [0, 0],
  declared: [range(0), range(0)],
  observed: [null, null],
});
function render(metrics, completed = 0, minute = 0) {
  const doc = new JSDOM(
    renderToStaticMarkup(
      React.createElement(InputCurves, {
        regions: [{ name: 'SC', total: 2, metrics }],
        completed,
        minute,
      }),
    ),
  ).window.document;
  return {
    doc,
    legend: [
      ...doc.querySelectorAll('[aria-label="输入曲线公共图例"] span'),
    ].map((e) => e.textContent.trim()),
  };
}

test('preview has one shared legend and retains genuine zero forecasts and guarantees', () => {
  const { doc, legend } = render([
    metric('P'),
    metric('Q'),
    metric('wind', true),
    metric('pv', true),
  ]);
  assert.deepEqual(legend, ['预测值', '负荷声明范围', '风光最低保证量']);
  assert.equal(
    doc.querySelectorAll('[aria-label="输入曲线公共图例"]').length,
    1,
  );
  assert.equal(
    doc.querySelectorAll('figure [aria-label="输入曲线公共图例"]').length,
    0,
  );
});

test('historical records without predictions omit that legend but retain recorded observations at cursor zero', () => {
  const metrics = [metric('P'), metric('wind', true)].map((m) => ({
    ...m,
    forecast: [null, null],
    observed: [range(0), null],
  }));
  const { legend } = render(metrics, 1, 0);
  assert.deepEqual(legend, ['负荷声明范围', '风光最低保证量', '观测值']);
});

test('observation legend follows committed data and does not imply future or missing observations exist', () => {
  const m = {
    ...metric('P'),
    declared: [null, null],
    observed: [null, range(0)],
  };
  const before = render([m], 1, 1);
  assert.deepEqual(before.legend, ['预测值']);
  assert.equal(
    before.doc
      .querySelector('[data-series="observed"]')
      .getAttribute('d')
      .trim(),
    '',
  );
  assert.deepEqual(render([m], 2, 2).legend, ['预测值', '观测值']);
  assert.deepEqual(
    render([{ ...m, forecast: [null, null], observed: [null, null] }], 2)
      .legend,
    [],
  );
});

test('load bands have distinct upper and lower boundaries, while renewable guarantees remain lower bounds only', () => {
  const load = { ...metric('P'), declared: [range(9, 11), range(10, 12)] };
  const { doc } = render([load, metric('wind', true)]);
  const figures = doc.querySelectorAll('figure');
  const lower = figures[0].querySelector('[data-series="declared-lower"]');
  const upper = figures[0].querySelector('[data-series="declared-upper"]');
  assert.ok(lower.getAttribute('d').length > 0);
  assert.ok(upper.getAttribute('d').length > 0);
  assert.notEqual(lower.getAttribute('d'), upper.getAttribute('d'));
  assert.ok(figures[0].querySelector('[data-series="declared-range"]'));
  assert.equal(
    figures[1].querySelector('[data-series="declared-upper"]'),
    null,
  );
  assert.ok(figures[1].querySelector('[data-series="declared-minimum"]'));
});
