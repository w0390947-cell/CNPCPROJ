import assert from 'node:assert/strict';
import fs from 'node:fs';
import { createRequire } from 'node:module';
import vm from 'node:vm';
import { test } from 'node:test';
import React from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import ts from 'typescript';

const require = createRequire(import.meta.url);
const source = fs.readFileSync(new URL('../../features/computation-quality/ui.tsx', import.meta.url), 'utf8');
const compiled = ts.transpileModule(source, {
  compilerOptions: { module: ts.ModuleKind.CommonJS, jsx: ts.JsxEmit.ReactJSX },
}).outputText;
const module = { exports: {} };
vm.runInNewContext(compiled, { exports: module.exports, require });
const { ComputationQualityDetails } = module.exports;
const quality = { policy: { relative_gap: 1e-4, version: 'quality-first-v1' }, reference_optimizations: {}, reference_coordination: [] };
const render = (recovery) => renderToStaticMarkup(React.createElement(ComputationQualityDetails, {
  quality,
  execution: { stages: [{ stage: 'day_ahead', regions: [{ region: 'A', ac_recovery: recovery }] }] },
}));

test('feasibility zero gap does not display as achieved economic precision', () => {
  const html = render({ outcome: 'certified', original_optimality: 'unproven', selected_attempt: 1,
    attempts: [{ phase: 'physical_feasibility', certified: true, solver_status: 'optimal', elapsed_seconds: .1, relative_gap: 0 }] });
  assert.match(html, /原经济目标最优性未证明/);
  assert.match(html, /此阶段的求解间隙不代表经济最优性/);
  assert.doesNotMatch(html, /AC 恢复通过，原目标达到规定精度/);
});

test('failed restoration and historical absence do not acquire certificates', () => {
  assert.match(render({ outcome: 'unresolved', original_optimality: 'unproven', attempts: [] }), /AC 恢复未获认证/);
  assert.doesNotMatch(render(undefined), /AC 可行恢复通过/);
});
