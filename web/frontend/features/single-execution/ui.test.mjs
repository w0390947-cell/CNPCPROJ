import assert from 'node:assert/strict';
import fs from 'node:fs';
import { createRequire } from 'node:module';
import vm from 'node:vm';
import { test } from 'node:test';
import React from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import ts from 'typescript';

const require = createRequire(import.meta.url);
const compiled = ts.transpileModule(
  fs.readFileSync(new URL('./ui.tsx', import.meta.url), 'utf8'),
  {
    compilerOptions: {
      module: ts.ModuleKind.CommonJS,
      jsx: ts.JsxEmit.ReactJSX,
    },
  },
).outputText;
const loaded = { exports: {} };
vm.runInNewContext(compiled, { exports: loaded.exports, require });
const { SingleExecutionPanel } = loaded.exports;

test('safe continuation cannot turn failed tracking and economics into passes', () => {
  const html = renderToStaticMarkup(
    React.createElement(SingleExecutionPanel, {
      execution: {
        version: 'single-execution-v6',
        completed_minutes: 30,
        total_minutes: 30,
        unified: {
          status: 'passed',
          tracking_status: 'violated',
          steps: [],
          plans: [],
          economic_proposals: [],
          target_admission_status: 'rejected',
          target_admissions: [
            {
              start_step: 0,
              end_step: 15,
              origin: 'external',
              requested_p_mw: 0.1,
              requested_q_mvar: 0,
              plan: null,
              reason: 'no certified witness',
            },
          ],
          domain: {
            p_band_mw: 0.05,
            q_band_mvar: 0.05,
            bound_source: 'declared fixture',
            research: {},
          },
        },
      },
      economicPassed: false,
      improvementPercent: 4.69,
    }),
  );
  assert.match(html, /安全与备用续接<\/td><td>通过/);
  assert.match(html, /硬任务未获保证/);
  assert.match(html, /PCC 期限后跟踪<\/td><td>未通过/);
  assert.match(html, /计划经济性改善至少 10%<\/td><td>未通过/);
  assert.match(html, /declared fixture/);
  assert.match(html, /存在未接纳目标/);
  assert.match(html, /接纳 0 \/ 1 个，拒绝 1 个/);
  assert.match(html, /no certified witness/);
});

test('unexecuted current result cannot claim safety or tracking', () => {
  const html = renderToStaticMarkup(
    React.createElement(SingleExecutionPanel, {
      execution: {
        version: 'single-execution-v6',
        status: 'not_computed',
        completed_minutes: 0,
        total_minutes: 30,
      },
    }),
  );
  assert.match(html, /尚未启动统一算法执行/);
  assert.doesNotMatch(html, /安全与备用续接<\/td><td>通过/);
});
