/** Real React DOM lifecycle tests in jsdom; widgets and network are ports.
 * Charts are ports by default; the power-chart test uses real Recharts at a fixed size.
 * This exercises actual dashboard hooks, effects, batching and DOM clicks.
 * It is not browser layout or network E2E coverage.
 */
import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import vm from 'node:vm';
import { fileURLToPath } from 'node:url';
import { createRequire } from 'node:module';
import { test } from 'node:test';
import { JSDOM } from 'jsdom';
import React, { act } from 'react';
import { createRoot } from 'react-dom/client';
import ts from 'typescript';

const frontend = fileURLToPath(new URL('../../', import.meta.url));
const require = createRequire(import.meta.url);
const first = JSON.parse(
  fs.readFileSync(
    new URL('../fixtures/synthetic-single-result.json', import.meta.url),
    'utf8',
  ),
);
const second = structuredClone(first);
second.metadata.generated_at = '2026-09-19T00:00:00+00:00';
const idA = 'a'.repeat(32),
  idB = 'b'.repeat(32);

for (const terminal of ['failed', 'cancelled', 'interrupted', 'succeeded']) {
  test(`restored rolling progress advances and preserves evidence after ${terminal}`, async () => {
    const running = {
      simulation_id: idA,
      state: 'running',
      stage: 'validating',
      stage_sequence: 4,
      total_stages: 6,
      result_available: false,
      stage_label: '滚动执行',
      rolling: {
        current_window: 2,
        completed_windows: 1,
        total_windows: 4,
        start_minute: 15,
        end_minute: 30,
        total_minutes: 60,
        phase: 'executing',
      },
    };
    const result = structuredClone(first);
    result.metadata.scenario_type = 'cluster_coordination';
    result.executive_summary.overall_passed = false;
    result.executive_summary.headline = '存在未通过项';
    const app = await mountDashboard({
      hydrate: true,
      view: 'cluster_coordination',
      result,
      runningStatus: running,
    });
    try {
      const feedback = () =>
        app.document.querySelector('.run-feedback').textContent;
      assert.match(feedback(), /当前窗口 2\/4.*已完成 1\/4/);
      assert.equal(
        app.document
          .querySelector('[role="progressbar"]')
          .getAttribute('aria-valuenow'),
        '54.2',
      );
      const completed = {
        ...running,
        rolling: {
          ...running.rolling,
          phase: 'completed',
          completed_windows: 2,
        },
      };
      await app.reportStatus(completed);
      assert.match(feedback(), /已完成 2\/4/);
      assert.equal(
        app.document
          .querySelector('[role="progressbar"]')
          .getAttribute('aria-valuenow'),
        '58.3',
      );
      if (terminal === 'succeeded') {
        await app.reportStatus({
          ...completed,
          stage: 'serializing',
          stage_sequence: 6,
          stage_label: '正在保存仿真结果',
        });
        assert.doesNotMatch(feedback(), /100%|任务已完成/);
        await app.reportStatus({
          ...completed,
          state: terminal,
          stage: terminal,
          stage_sequence: 6,
          result_available: true,
        });
        await app.finish('succeeded', result);
        assert.match(feedback(), /任务已完成.*存在未通过项/);
        assert.match(feedback(), /100%/);
      } else {
        await app.reportStatus({
          ...completed,
          state: terminal,
          stage: terminal,
          stage_label: `任务${terminal}`,
        });
        await app.finish('failed');
        assert.doesNotMatch(feedback(), /当前窗口|运行中/);
        assert.match(feedback(), /最后记录窗口 2\/4.*已完成 2\/4/);
      }
      assert.equal(app.document.querySelector('[role="progressbar"]'), null);
    } finally {
      await app.close();
    }
  });
}

test('seven-bus results retain functional cards and replay regional plan totals', async () => {
  const recorded = structuredClone(first);
  recorded.topology_nodes = [
    'PCC',
    'MAIN',
    'WT1',
    'WT2',
    'PV1',
    'PV2',
    'FLEX',
  ].map((name) => ({
    id: `SC_${name}`,
    label: name,
    kind: name.startsWith('WT')
      ? 'wind'
      : name.startsWith('PV')
        ? 'pv'
        : name.toLowerCase(),
  }));
  recorded.topology_edges = recorded.topology_nodes
    .slice(1)
    .map((node, index) => ({
      id: `branch-${index}`,
      source: index === 0 ? 'SC_PCC' : 'SC_MAIN',
      target: node.id,
    }));
  recorded.timeseries = [0, 1, 2].map((minute) => ({
    ...first.timeseries[0],
    time_hour: minute / 60,
    p_grid_optimized_mw: 6 + minute,
    load_mw: 14 + minute,
    wind_used_mw: 7 + minute,
    wind_q_mvar: -0.5 + minute * 0.5,
    pv_used_mw: 3 + minute,
    storage_discharge_mw: minute,
    storage_charge_mw: 1 - minute,
    svg_q_mvar: -0.5 + minute,
  }));
  const app = await mountDashboard({ hydrate: true, result: recorded });
  try {
    const diagram = app.document.querySelector('.single-network');
    assert.ok(diagram);
    assert.equal(diagram.querySelectorAll('.single-device').length, 5);
    assert.equal(diagram.querySelectorAll('.single-link').length, 4);
    assert.match(diagram.getAttribute('aria-label'), /功能分组示意/);
    const values = () =>
      [...diagram.querySelectorAll('.single-device b')].map(
        (element) => element.textContent,
      );
    assert.deepEqual(values(), [
      '6.00 MW',
      '全网负荷 14.00',
      'P：7.00 MW / Q：-0.50 Mvar',
      '3.00 MW',
      '-1.00 MW / -0.50 Mvar',
    ]);
    await app.click('[aria-label="下一分钟记录"]');
    assert.deepEqual(values(), [
      '7.00 MW',
      '全网负荷 15.00',
      'P：8.00 MW / Q：0.00 Mvar',
      '4.00 MW',
      '1.00 MW / 0.50 Mvar',
    ]);
    await app.click('[aria-label="下一分钟记录"]');
    assert.equal(values()[2], 'P：9.00 MW / Q：0.50 Mvar');
    await app.click('[aria-label="回到起点"]');
    assert.equal(values()[2], 'P：7.00 MW / Q：-0.50 Mvar');
    assert.deepEqual((await app.exported()).body, recorded);
  } finally {
    await app.close();
  }
});

test('single power chart renders six series and noncumulative tooltip values with real Recharts', async () => {
  const recorded = structuredClone(first);
  recorded.timeseries = [0, 1, 2].map((hour) => ({
    ...first.timeseries[0],
    time_hour: hour,
    load_mw: 10,
    p_grid_optimized_mw: 6.1,
    wind_used_mw: 4,
    pv_used_mw: 2,
    storage_discharge_mw: 0,
    storage_charge_mw: 2,
    active_loss_mw: 0.1,
  }));
  const app = await mountDashboard({
    hydrate: true,
    result: recorded,
    realCharts: true,
  });
  try {
    const panel = app.document.querySelector('.chart-panel');
    assert.match(panel.textContent, /负荷、风光储与 PCC 受电/);
    assert.deepEqual(
      [...panel.querySelectorAll('li')].map((e) => e.textContent),
      ['负荷', 'PCC 受电', '风电', '光伏', '储能放电', '储能充电（零轴下方）'],
    );
    assert.equal(panel.querySelectorAll('.recharts-area').length, 4);
    assert.equal(panel.querySelectorAll('.recharts-line').length, 2);
    const surface = panel.querySelector('svg[role="application"]');
    assert.ok(surface);
    await act(async () => surface.focus());
    const details = [...panel.querySelectorAll('dl > div')].map(
      (e) => e.textContent,
    );
    assert.deepEqual(details, [
      '负荷10.00 MW',
      'PCC 受电6.10 MW',
      '风电4.00 MW',
      '光伏2.00 MW',
      '储能放电0.00 MW',
      '储能充电2.00 MW',
      '有功网损0.10 MW',
    ]);
    assert.deepEqual((await app.exported()).body, recorded);
  } finally {
    await app.close();
  }
});

test('detailed power panel is single-only and preserves explicit missing-data states', async () => {
  const recorded = structuredClone(first);
  delete recorded.timeseries[0].pv_used_mw;
  delete recorded.timeseries[0].active_loss_mw;
  const app = await mountDashboard({ hydrate: true, result: recorded });
  try {
    assert.match(
      app.document.querySelector('.chart-panel').textContent,
      /部分计划数据缺失/,
    );
    assert.deepEqual((await app.exported()).body, recorded);
  } finally {
    await app.close();
  }
  const overview = await mountDashboard({ view: 'overview', hydrate: true });
  try {
    const panel = overview.document.querySelector('.chart-panel');
    assert.match(panel.textContent, /负荷、新能源与 PCC 受电/);
    assert.doesNotMatch(panel.textContent, /储能充电（零轴下方）/);
  } finally {
    await overview.close();
  }
});

async function mountDashboard({
  hydrate = false,
  view = 'single_microgrid',
  result = first,
  health = 'ok',
  realCharts = false,
  runningStatus = null,
} = {}) {
  const dom = new JSDOM('<div id="root"></div>', {
    url: `http://localhost/${view === 'overview' ? '' : view.replaceAll('_', '-')}${hydrate ? `?simulation=${idA}` : ''}`,
  });
  const previous = {
    window: globalThis.window,
    document: globalThis.document,
    act: globalThis.IS_REACT_ACT_ENVIRONMENT,
  };
  globalThis.window = dom.window;
  globalThis.document = dom.window.document;
  globalThis.IS_REACT_ACT_ENVIRONMENT = true;
  const downloads = [],
    requests = [],
    blobs = new Map(),
    cache = new Map();
  let pending,
    count = hydrate ? 1 : 0;
  class CaptureURL extends URL {
    static createObjectURL(blob) {
      const key = `blob:audit-${blobs.size}`;
      blobs.set(key, blob);
      return key;
    }
    static revokeObjectURL(key) {
      blobs.delete(key);
    }
  }
  dom.window.HTMLAnchorElement.prototype.click = function () {
    downloads.push({ filename: this.download, blob: blobs.get(this.href) });
  };
  const widget = (button = false) =>
    function Widget({
      children,
      className,
      disabled,
      onClick,
      role,
      'aria-label': ariaLabel,
    }) {
      return React.createElement(
        button ? 'button' : 'div',
        { className, disabled, onClick, role, 'aria-label': ariaLabel },
        children,
      );
    };
  const Icon = () => null;
  const fetch = async (_url, init) => {
    assert.equal(init.method, 'POST');
    requests.push(JSON.parse(init.body));
    count++;
    return new Response(
      JSON.stringify({
        simulation_id: count === 1 ? idA : idB,
        state: 'running',
        name: 'Synthetic identity test',
      }),
    );
  };
  function load(file) {
    if (cache.has(file)) return cache.get(file);
    // An explicit old-source path allows a negative control without editing production.
    const sourcePath =
      file === 'components/simulation-dashboard.tsx' &&
      process.env.DASHBOARD_TEST_SOURCE
        ? process.env.DASHBOARD_TEST_SOURCE
        : path.join(frontend, file);
    const compiled = ts.transpileModule(fs.readFileSync(sourcePath, 'utf8'), {
      compilerOptions: {
        module: ts.ModuleKind.CommonJS,
        target: ts.ScriptTarget.ES2022,
        jsx: ts.JsxEmit.ReactJSX,
      },
    }).outputText;
    const loadedModule = { exports: {} };
    vm.runInNewContext(
      compiled,
      {
        module: loadedModule,
        exports: loadedModule.exports,
        console,
        process: { env: {} },
        window: dom.window,
        document: dom.window.document,
        URL: CaptureURL,
        Blob,
        Response,
        AbortController,
        AbortSignal,
        fetch,
        crypto: globalThis.crypto,
        setTimeout,
        clearTimeout,
        require(name) {
          if (name === 'react' || name === 'react/jsx-runtime')
            return require(name);
          if (name === 'next/navigation')
            return { useRouter: () => ({ push() {} }) };
          if (name === '@/lib/simulation-api') return api;
          if (name === '@/lib/simulation-presentation')
            return load('lib/simulation-presentation.ts');
          if (name === '@/shared/ui/platform-chrome')
            return load('shared/ui/platform-chrome.tsx');
          if (name === '@/shared/lib/region-presentation')
            return load('shared/lib/region-presentation.ts');
          if (name === '@/features/simulation-events')
            return load('features/simulation-events/index.ts');
          if (name === '@/features/cluster-coordination')
            return load('features/cluster-coordination/index.ts');
          if (name === '@/features/computation-quality')
            return load('features/computation-quality/index.ts');
          if (name === '@/features/network-topology')
            return load('features/network-topology/index.ts');
          if (name === '@/features/active-power-chart')
            return load('features/active-power-chart/index.ts');
          if (name === '@/features/simulation-progress')
            return load('features/simulation-progress/index.ts');
          if (name.endsWith('.module.css'))
            return {
              default: new Proxy({}, { get: (_target, key) => key }),
              __esModule: true,
            };
          if (name.startsWith('./')) {
            const relative = path.posix.join(path.posix.dirname(file), name);
            const extension = ['.ts', '.tsx'].find((ext) =>
              fs.existsSync(path.join(frontend, relative + ext)),
            );
            assert.ok(extension, `Missing relative import ${relative}`);
            return load(relative + extension);
          }
          if (name === 'lucide-react')
            return new Proxy({}, { get: () => Icon });
          if (name === 'recharts') {
            if (realCharts)
              return {
                ...require('recharts'),
                ResponsiveContainer: ({ children }) =>
                  React.cloneElement(children, { width: 760, height: 270 }),
              };
            return new Proxy({}, { get: () => Icon });
          }
          if (name === '@/components/ui/label')
            return {
              Label: ({ children, htmlFor }) =>
                React.createElement('label', { htmlFor }, children),
            };
          if (name === '@/components/ui/progress')
            return {
              Progress: ({ value, className, ...props }) =>
                React.createElement('div', {
                  ...props,
                  className,
                  role: 'progressbar',
                  'aria-valuenow': value,
                }),
            };
          // Model the Select port with a native select so real dashboard callbacks run.
          if (name === '@/components/ui/select')
            return {
              Select({ value, onValueChange, children, disabled }) {
                const trigger = React.Children.toArray(children).find(
                  (child) => child.props?.id,
                );
                return React.createElement(
                  'select',
                  {
                    id: trigger?.props.id,
                    value,
                    disabled,
                    onChange: (event) => onValueChange(event.target.value),
                  },
                  children,
                );
              },
              SelectTrigger: () => null,
              SelectValue: () => null,
              SelectContent: ({ children }) => children,
              SelectItem: ({ children, value }) =>
                React.createElement('option', { value }, children),
            };
          if (name.startsWith('@/components/ui/'))
            return new Proxy(
              {},
              { get: (_target, key) => widget(key === 'Button') },
            );
          throw new Error(`Unexpected import ${name}`);
        },
      },
      { filename: file },
    );
    cache.set(file, loadedModule.exports);
    return loadedModule.exports;
  }
  const actual = load('lib/simulation-api.ts');
  const api = {
    ...actual,
    getHealth: async () => ({ status: health }),
    getSimulation: async () =>
      runningStatus ?? { simulation_id: idA, state: 'succeeded' },
    getSimulationResult: async () => structuredClone(result),
    waitForSimulation: async (status, onStatus) => {
      onStatus({ ...status, stage_label: 'Synthetic job', total_stages: 6 });
      if (count === 1 && !runningStatus) {
        onStatus({ ...status, state: 'succeeded', result_available: true });
        return structuredClone(result);
      }
      return new Promise((resolve, reject) => {
        pending = { resolve, reject, onStatus };
      });
    },
    cancelSimulation: async () => {
      pending.reject(new actual.SimulationCancelledError());
      return { simulation_id: idB, state: 'cancelled' };
    },
  };
  const Dashboard = load('components/simulation-dashboard.tsx').default;
  const root = createRoot(dom.window.document.getElementById('root'));
  await act(async () => {
    root.render(React.createElement(Dashboard, { initialView: view }));
  });
  return {
    document: dom.window.document,
    requests,
    async reportStatus(status) {
      await act(async () => pending.onStatus(status));
    },
    async changeSelect(selector, value) {
      const select = dom.window.document.querySelector(selector);
      assert.ok(select, selector);
      assert.equal(select.disabled, false);
      await act(async () => {
        select.value = value;
        select.dispatchEvent(new dom.window.Event('change', { bubbles: true }));
      });
    },
    async click(selector) {
      const button = dom.window.document.querySelector(selector);
      assert.ok(button, selector);
      assert.equal(button.matches(':disabled'), false);
      await act(async () => {
        button.click();
      });
    },
    async clickButton(label) {
      const button = [...dom.window.document.querySelectorAll('button')].find(
        (candidate) => candidate.textContent.trim() === label,
      );
      assert.ok(button, label);
      assert.equal(button.disabled, false);
      await act(async () => {
        button.click();
      });
    },
    async finish(outcome, nextResult = second) {
      await act(async () => {
        if (outcome === 'failed')
          pending.reject(new Error('Controlled worker failure'));
        else pending.resolve(structuredClone(nextResult));
      });
    },
    async exported() {
      await this.click('.export-button');
      const download = downloads.at(-1);
      return {
        filename: download.filename,
        body: JSON.parse(await download.blob.text()),
      };
    },
    async close() {
      await act(async () => {
        root.unmount();
      });
      dom.window.close();
      globalThis.window = previous.window;
      globalThis.document = previous.document;
      globalThis.IS_REACT_ACT_ENVIRONMENT = previous.act;
    },
  };
}

for (const outcome of ['cancelled', 'failed', 'succeeded']) {
  test(`completed A remains identifiable while B runs and then ${outcome}`, async () => {
    const app = await mountDashboard();
    try {
      assert.equal(app.document.querySelector('.export-button').disabled, true);
      assert.deepEqual(
        [...app.document.querySelectorAll('.single-device b')].map(
          (element) => element.textContent,
        ),
        ['— MW', '全网负荷 —', 'P：— MW / Q：— Mvar', '— MW', '— MW / — Mvar'],
      );
      await app.click('.run-button');
      assert.equal(
        app.document.querySelector('.single-wind b').textContent,
        `P：${first.timeseries[0].wind_used_mw.toFixed(2)} MW / Q：— Mvar`,
      );
      assert.deepEqual(await app.exported(), {
        filename: `oilfield-single_microgrid-${idA}.json`,
        body: first,
      });
      await app.click('.run-button');
      assert.deepEqual(await app.exported(), {
        filename: `oilfield-single_microgrid-${idA}.json`,
        body: first,
      });
      assert.match(
        app.document.querySelector('.result-origin').textContent,
        /上次完成的结果/,
      );
      if (outcome === 'cancelled') await app.click('.run-button');
      else await app.finish(outcome);
      const success = outcome === 'succeeded';
      assert.deepEqual(await app.exported(), {
        filename: `oilfield-single_microgrid-${success ? idB : idA}.json`,
        body: success ? second : first,
      });
      const origin = app.document.querySelector('.result-origin').textContent;
      assert.ok(origin.includes(success ? idB : idA));
      assert.equal(origin.includes('上次完成的结果'), !success);
      assert.equal(
        Boolean(app.document.querySelector('[role="alert"]')),
        outcome === 'failed',
      );
    } finally {
      await app.close();
    }
  });
}

test('configuration uses independent controls and reports the actual run limits', async () => {
  const app = await mountDashboard();
  try {
    assert.equal(app.document.querySelector('.profile-picker'), null);
    assert.equal(
      app.document
        .querySelector('.config-body')
        .textContent.includes('基础参数'),
      false,
    );
    assert.match(
      app.document.querySelector('.configuration-summary b').textContent,
      /8 时段\s*·\s*储能启用\s*·\s*MISOCP/,
    );
    assert.equal(
      app.document.querySelector('[aria-label="单次求解时限，秒"]'),
      null,
    );
    assert.ok(app.document.querySelector('#steps-select'));
    assert.equal(
      app.document.querySelector('[aria-label="求解质量与计算预算"]'),
      null,
    );
  } finally {
    await app.close();
  }
});

test('wind surge button submits a bounded branch event from the current playback time', async () => {
  const app = await mountDashboard({ hydrate: true });
  try {
    const eventButtons = [
      ...app.document.querySelectorAll('[aria-label="新能源事件注入"] button'),
    ].map((button) => button.textContent.trim());
    assert.deepEqual(eventButtons, [
      '当前时刻注入光伏大发',
      '当前时刻注入风电大发',
    ]);

    await app.clickButton('当前时刻注入风电大发');
    assert.equal(app.requests.length, 1);
    const request = app.requests[0];
    assert.equal(request.scenario_type, 'single_microgrid');
    assert.equal(request.region, first.request.region);
    assert.equal(request.steps, first.request.steps);
    assert.equal(request.storage_enabled, first.request.storage_enabled);
    assert.equal(request.solver.time_limit_seconds, undefined);
    assert.equal(request.solver.quality_policy, 'quality-first-v1');
    assert.equal(request.events.length, 1);
    const { event_id: eventId, ...event } = request.events[0];
    assert.match(eventId, /^[0-9a-f-]{36}$/);
    assert.deepEqual(event, {
      event_type: 'wind_surge',
      target: 'SC',
      time_axis: 'clock_minute',
      start: 0,
      end: 180,
      magnitude: 1.35,
      label: '山城参数化微网（SC）00:00 起风电大发',
    });
    await app.finish('succeeded');
  } finally {
    await app.close();
  }
});

test('historical URL hydration commits result and its identity together', async () => {
  const app = await mountDashboard({ hydrate: true });
  try {
    assert.deepEqual(await app.exported(), {
      filename: `oilfield-single_microgrid-${idA}.json`,
      body: first,
    });
    await app.click('.run-button');
    await app.finish('failed');
    assert.deepEqual(await app.exported(), {
      filename: `oilfield-single_microgrid-${idA}.json`,
      body: first,
    });
  } finally {
    await app.close();
  }
});

// Synthetic UI/transport fixture: these values exercise scope and request identity,
// not numerical validation. No solver output is inferred from this projection.
function clusterFixture() {
  const result = structuredClone(first);
  result.metadata.scenario_type = 'cluster_coordination';
  result.metadata.region = 'SC,YA_B,YA_C';
  result.request.scenario_type = 'cluster_coordination';
  result.request.region = 'YA_C';
  result.request.events = [
    {
      event_id: 'baseline-load-event',
      event_type: 'load_surge',
      target: 'SC',
      time_axis: 'clock_minute',
      start: 0,
      end: 180,
      magnitude: 1.1,
      label: 'Synthetic existing event',
    },
  ];
  result.cluster_timeseries = [0, 3].map((time_hour) => ({
    time_hour,
    aggregate_load_mw: 15,
    aggregate_renewable_mw: 6,
    aggregate_import_mw: 9,
    aggregate_q_mvar: 1,
    import_limit_mw: 26,
    regional_import_mw: { SC: 3, YA_B: 3, YA_C: 3 },
  }));
  return result;
}

function coordinationFixture() {
  const result = clusterFixture();
  result.validation_items.push({
    code: 'ADMM_CONVERGENCE',
    label: '协调收敛',
    passed: true,
    explanation: '',
  });
  result.cluster_timeseries[1].regional_import_mw = { SC: 4, YA_B: 5, YA_C: 6 };
  result.cluster_timeseries[1].aggregate_import_mw = 15;
  result.admm_history = [1, 2, 3].map((iteration) => ({
    iteration,
    primal_residual: 1 / iteration,
    dual_residual: 0.5 / iteration,
    primal_tolerance: 0.1,
    dual_tolerance: 0.1,
    convergence_streak: iteration,
    fresh_region_count: 3,
    fallback_regions: [],
    coordination: {
      version: 'coordination-trace-v1',
      communication_tick: iteration,
      coordination_epoch: iteration - 1,
      global_updated: true,
      time_hours: [0, 3],
      regions: ['SC', 'YA_B', 'YA_C'].map((region) => ({
        region,
        response_epoch: iteration - 1,
        response_sent_tick: iteration,
        fresh: true,
        outage: false,
        fallback: false,
        proposal_p_mw: [iteration + 0.25, iteration + 10.25],
        proposal_q_mvar: [0.5, 0.6],
        reference_p_mw: [iteration, iteration + 10],
        reference_q_mvar: [0.4, 0.5],
      })),
    },
  }));
  return result;
}

const clusterPanel = '[aria-label="跨区域微电网集群"]';

test('one cluster run presents automatic execution stages and retains unknown evidence', async () => {
  const result = coordinationFixture();
  result.executive_summary.overall_passed = false;
  result.cluster_execution = {
    version: 'cluster-execution-v1',
    status: 'unknown',
    storage_enabled: true,
    policy: { source: '软件仿真容差：0.05 MW/Mvar' },
    input_basis: '同版本模拟输入',
    stages: [
      { stage: 'day_ahead', status: 'passed' },
      { stage: 'intraday', status: 'unknown' },
      { stage: 'minute', status: 'not_computed' },
    ],
  };
  result.validation_items.push(
    {
      code: 'INTRADAY',
      label: '日内执行',
      passed: false,
      assessment_status: 'unknown',
      explanation: '区域求解超时',
    },
    {
      code: 'MINUTE',
      label: '分钟执行',
      passed: false,
      assessment_status: 'not_computed',
      explanation: '缺少日内计划',
    },
  );
  const app = await mountDashboard({ view: 'cluster_coordination', result });
  try {
    await app.click('.run-button');
    assert.equal(app.requests.length, 1);
    await app.clickButton('执行校核');
    const panel = app.document.querySelector('[aria-label="自动执行校核"]');
    assert.ok(panel);
    assert.deepEqual(
      [...panel.querySelectorAll('li b')].map((e) => e.textContent),
      ['通过', '无法确认', '未执行'],
    );
    assert.equal(panel.querySelectorAll('button').length, 0);
    assert.match(
      app.document.querySelector('.verdict-panel').textContent,
      /无法确认/,
    );
    assert.match(
      app.document.querySelector('.verdict-panel').textContent,
      /未执行/,
    );
    assert.deepEqual(
      (await app.exported()).body.cluster_execution,
      result.cluster_execution,
    );
  } finally {
    await app.close();
  }
});

test('completed cluster opens plan, then coordination opens last round and replays power on an independent planning axis', async () => {
  const app = await mountDashboard({
    view: 'cluster_coordination',
    result: coordinationFixture(),
  });
  try {
    assert.match(
      app.document.querySelector(clusterPanel).textContent,
      /运行后显示/,
    );
    await app.click('.run-button');
    assert.match(
      app.document.querySelector('.current-time').textContent,
      /00:00/,
    );
    await app.clickButton('协调过程');
    assert.match(
      app.document.querySelector('.current-time').textContent,
      /第 3 轮/,
    );
    const card = () =>
      app.document.querySelector(`${clusterPanel} details [data-region="SC"]`)
        .textContent;
    const diagram = app.document.querySelector(
      `${clusterPanel} .topology-canvas`,
    );
    assert.ok(diagram.querySelector('.grid-source'));
    assert.equal(diagram.querySelectorAll('.trunk').length, 3);
    assert.equal(diagram.querySelectorAll('.region-node').length, 3);
    const mainCard = () => diagram.querySelector('[data-region="SC"]');
    assert.ok(mainCard().querySelector('.region-flow'));
    assert.match(mainCard().textContent, /协调器参考 P3\.00 MW/);
    assert.equal(mainCard().textContent.includes('申报 − 参考'), false);
    const details = app.document.querySelector(`${clusterPanel} details`);
    assert.equal(details.open, false);
    await app.click(`${clusterPanel} details > summary`);
    assert.equal(details.open, true);
    assert.match(card(), /3\.25 MW/);
    assert.match(card(), /3\.00 MW/);
    assert.match(card(), /0\.25 MW/);
    await app.changeSelect('[aria-label="观察计划时刻"]', '3');
    assert.match(card(), /13\.00 MW/);
    assert.match(mainCard().textContent, /13\.00 MW/);
    assert.match(
      app.document.querySelector('.current-time').textContent,
      /第 3 轮/,
    );
    await app.clickButton('回放协调过程');
    await app.click('[aria-label="暂停回放"]');
    assert.match(
      app.document.querySelector('.current-time').textContent,
      /第 1 轮/,
    );
    assert.match(card(), /11\.00 MW/);
    await app.click('[aria-label="下一轮记录"]');
    assert.match(card(), /12\.00 MW/);
    assert.match(mainCard().textContent, /12\.00 MW/);
    assert.match(
      app.document.querySelector(clusterPanel).textContent,
      /计划时刻 03:00/,
    );
    await app.clickButton('运行态势');
    assert.match(card(), /PCC 计划3\.00 MW/);
    assert.match(mainCard().textContent, /PCC 计划3\.00 MW/);
    await app.click('[aria-label="下一计划时刻"]');
    assert.match(card(), /PCC 计划4\.00 MW/);
    assert.match(mainCard().textContent, /PCC 计划4\.00 MW/);
    assert.match(
      diagram.querySelector('.grid-source').textContent,
      /15\.00 MW/,
    );
    await app.click(`${clusterPanel} details > summary`);
    assert.equal(details.open, false);
    assert.match(
      app.document.querySelector(clusterPanel).textContent,
      /15\.00 MW \/ 26\.00 MW/,
    );
    assert.match(
      app.document.querySelector('.current-time').textContent,
      /03:00/,
    );
    assert.equal(app.requests.length, 1); // Neither axis changes the immutable run.
    assert.deepEqual((await app.exported()).body, coordinationFixture());
  } finally {
    await app.close();
  }
});

test('historical history explicitly lacks per-round power but retains final PCC plans', async () => {
  const result = coordinationFixture();
  for (const record of result.admm_history) delete record.coordination;
  const app = await mountDashboard({
    hydrate: true,
    view: 'cluster_coordination',
    result,
  });
  try {
    const panel = () => app.document.querySelector(clusterPanel).textContent;
    await app.clickButton('协调过程');
    assert.match(panel(), /本次结果未记录逐轮功率/);
    assert.match(
      app.document.querySelector('.current-time').textContent,
      /第 3 轮/,
    );
    await app.clickButton('运行态势');
    assert.match(panel(), /PCC 计划3\.00 MW/);
    assert.equal(panel().includes('本次结果未记录逐轮功率'), false);
  } finally {
    await app.close();
  }
});

test('nonconverged results retain last references and never claim a converged plan', async () => {
  const result = coordinationFixture();
  result.validation_items.find(
    (item) => item.code === 'ADMM_CONVERGENCE',
  ).passed = false;
  const app = await mountDashboard({
    hydrate: true,
    view: 'cluster_coordination',
    result,
  });
  try {
    assert.match(
      app.document.querySelector(clusterPanel).textContent,
      /未收敛 · 最后一轮参考/,
    );
    await app.clickButton('运行态势');
    assert.match(
      app.document.querySelector(clusterPanel).textContent,
      /未收敛 · 最后一轮参考/,
    );
    assert.match(
      app.document.querySelector(clusterPanel).textContent,
      /PCC 计划3\.00 MW/,
    );
  } finally {
    await app.close();
  }
});

test('communication trace distinguishes stale, outage and fallback; final plan cannot inject iterations', async () => {
  const result = coordinationFixture();
  result.metadata.scenario_type = 'communication_fault';
  const record = result.admm_history.at(-1);
  record.coordination.global_updated = false;
  record.fresh_region_count = 2;
  Object.assign(record.coordination.regions[1], {
    fresh: false,
    outage: true,
    fallback: true,
    response_epoch: 0,
    response_sent_tick: 1,
  });
  const app = await mountDashboard({
    hydrate: true,
    view: 'communication_fault',
    result,
  });
  try {
    const text = app.document.querySelector(
      `${clusterPanel} details [data-region="YA_B"]`,
    ).textContent;
    assert.match(text, /失联 · 自治降级 · 沿用历史申报/);
    const faultCard = app.document.querySelector(
      `${clusterPanel} .region-node[data-region="YA_B"]`,
    );
    assert.ok(faultCard.classList.contains('faulted'));
    assert.match(
      faultCard.querySelector('.fault-dot').textContent,
      /失联 · 自治降级/,
    );
    assert.match(
      app.document.querySelector(clusterPanel).textContent,
      /等待有效区域响应，参考保持/,
    );
    await app.clickButton('最终计划');
    for (const button of app.document.querySelectorAll('.inject-button'))
      assert.equal(button.disabled, true);
    await app.clickButton('协调过程');
    for (const button of app.document.querySelectorAll('.inject-button'))
      assert.equal(button.disabled, false);
  } finally {
    await app.close();
  }
});

const eventArea = '[aria-label="新能源事件注入"]';

for (const qualityView of ['cluster_coordination', 'single_microgrid']) {
  test(`${qualityView} quality evidence keeps exhaustion distinct and preserves exports`, async () => {
    const result =
      qualityView === 'single_microgrid'
        ? structuredClone(first)
        : coordinationFixture();
    result.computation_quality = {
      policy: {
        version: 'quality-first-v1',
        relative_gap: 0.0001,
        solve_seconds: [180, 600, 1800],
        admm_iterations: [360, 720, 1000],
      },
      reference_optimizations: {
        optimized: [
          {
            status: 'budget_exhausted',
            target_relative_gap: 0.0001,
            selected_attempt: 1,
            attempts: [
              {
                budget_seconds: 1800,
                solver_status: 'timelimit',
                feasible: true,
                relative_gap: 0.02,
              },
            ],
          },
        ],
      },
      reference_coordination: [
        {
          iteration_budget: 1000,
          completed_iterations: 1000,
          converged: false,
          primal_residual: 1,
          dual_residual: 2,
          primal_tolerance: 0.01,
          dual_tolerance: 0.02,
        },
      ],
    };
    if (qualityView === 'single_microgrid')
      result.computation_quality.reference_coordination = [];
    const app = await mountDashboard({
      view: qualityView,
      hydrate: true,
      result,
    });
    try {
      if (qualityView === 'cluster_coordination')
        await app.clickButton('执行校核');
      const text = app.document.querySelector(
        '[aria-label="求解质量与计算预算"]',
      ).textContent;
      assert.match(text, /预算耗尽，尚未达到规定精度/);
      if (qualityView === 'cluster_coordination')
        assert.match(text, /1000.*未收敛/);
      else {
        assert.match(text, /单微网优化/);
        assert.doesNotMatch(text, /ADMM|协调分级上限|协调范围|日前协调/);
      }
      assert.match(text, /0.01%/);
      assert.deepEqual((await app.exported()).body, result);
    } finally {
      await app.close();
    }
  });
}

for (const view of ['overview', 'cluster_coordination']) {
  test(`${view} restores one task across plan, coordination and execution without recalculation`, async () => {
    const result = coordinationFixture();
    const app = await mountDashboard({ view, hydrate: true, result });
    try {
      assert.match(
        app.document.querySelector('.current-time').textContent,
        /原始计划时刻.*00:00/,
      );
      assert.equal(app.document.querySelector('#steps-select'), null);
      await app.changeSelect(`${eventArea} select`, 'YA_B');
      await app.clickButton('协调过程');
      assert.match(
        app.document.querySelector('.current-time').textContent,
        /第 3 轮/,
      );
      assert.ok(app.document.querySelector(eventArea).closest('[hidden]'));
      await app.clickButton('执行校核');
      assert.match(
        app.document.querySelector('[aria-label="集群执行校核"]').textContent,
        /未记录执行校核证据/,
      );
      assert.equal(app.document.querySelector('.playback-bar'), null);
      assert.equal(app.document.querySelector('.chart-panel'), null);
      assert.equal(app.document.querySelector('.metrics-grid'), null);
      assert.equal(app.document.querySelectorAll('.verdict-panel').length, 1);
      assert.equal(app.document.querySelectorAll('.run-button').length, 1);
      await app.clickButton('运行态势');
      assert.equal(
        app.document.querySelector(`${eventArea} select`).value,
        'YA_B',
      );
      assert.equal(app.document.querySelector('#steps-select'), null);
      assert.match(
        app.document.querySelector('.result-origin').textContent,
        new RegExp(`全天 ${result.metadata.steps} 个时段`),
      );
      assert.equal(app.requests.length, 0);
      assert.deepEqual((await app.exported()).body, result);
    } finally {
      await app.close();
    }
  });
}

test('execution windows keep stopped and missing feedback distinct from completion', async () => {
  const result = coordinationFixture();
  result.cluster_execution = {
    status: 'unknown',
    storage_enabled: true,
    policy: { update_minutes: 15, horizon_minutes: 240 },
    input_basis: 'Synthetic stopped-window fixture',
    stages: [],
    rolling_updates: [
      {
        start_minute: 0,
        end_minute: 15,
        horizon_end_minute: 240,
        status: 'passed',
        adopted: true,
        actual_end_energy_mwh: { SC: 1, YA_B: 1, YA_C: 1 },
        admm_iterations: 5,
      },
      {
        start_minute: 15,
        end_minute: 30,
        horizon_end_minute: 255,
        status: 'unknown',
        adopted: false,
        actual_end_energy_mwh: {},
        reason: '区域求解超时',
      },
    ],
  };
  const app = await mountDashboard({
    view: 'cluster_coordination',
    hydrate: true,
    result,
  });
  try {
    await app.clickButton('执行校核');
    const panel = app.document.querySelector('[aria-label="集群执行校核"]');
    assert.match(panel.textContent, /已采用 1 个窗口/);
    const rows = [...panel.querySelectorAll('tbody tr')].map(
      (row) => row.textContent,
    );
    assert.match(rows[0], /00:00—00:15.*通过已采用已记录5/);
    assert.match(
      rows[1],
      /00:15—00:30.*无法确认未采用未记录未记录区域求解超时/,
    );
    assert.equal(app.requests.length, 0);
    assert.deepEqual((await app.exported()).body, result);
  } finally {
    await app.close();
  }
});

for (const terminal of ['failed', 'cancelled']) {
  test(`unified cluster keeps result identity after a new task is ${terminal}`, async () => {
    const result = coordinationFixture();
    const app = await mountDashboard({
      view: 'cluster_coordination',
      hydrate: true,
      result,
    });
    try {
      await app.clickButton('执行校核');
      await app.click('.run-button');
      for (const button of app.document.querySelectorAll(
        '[aria-label="集群结果视图"] button',
      )) {
        assert.equal(button.disabled, true);
      }
      assert.deepEqual((await app.exported()).body, result);
      if (terminal === 'cancelled') await app.click('.run-button');
      else await app.finish('failed');
      await app.clickButton('运行态势');
      assert.match(
        app.document.querySelector('.result-origin').textContent,
        /上次完成的结果/,
      );
      assert.deepEqual((await app.exported()).body, result);
      assert.equal(app.requests.length, 1);
    } finally {
      await app.close();
    }
  });
}

test('cluster coordination declares all regions with one navigation entry and renewable controls', async () => {
  const app = await mountDashboard({
    view: 'cluster_coordination',
    result: clusterFixture(),
  });
  try {
    assert.match(
      app.document.querySelector('.configuration-summary b').textContent,
      /^基础优化 15 分钟／时段（全天 96 个时段）\s*·\s*储能启用\s*·\s*MISOCP · ADMM$/,
    );
    assert.equal(app.document.querySelector('#region-select'), null);
    assert.ok(app.document.querySelector(eventArea));
    assert.equal(
      app.document.querySelectorAll('nav button[aria-label="集群协调"]').length,
      1,
    );
    assert.equal(
      app.document.querySelectorAll('nav button[aria-label="综合态势"]').length,
      0,
    );
    const text = app.document.querySelector('.config-body').textContent;
    assert.match(text, /协调范围/);
    for (const code of ['SC', 'YA_B', 'YA_C']) assert.ok(text.includes(code));
    assert.equal(text.includes('重点展示区域'), false);
    await app.click('.run-button');
    assert.equal(app.requests[0].scenario_type, 'cluster_coordination');
    assert.equal(app.requests[0].steps, 96);
    assert.equal(app.requests[0].solver.quality_policy, 'quality-first-v1');
    assert.equal(
      app.document.querySelector('[aria-label="单次求解时限，秒"]'),
      null,
    );
    assert.equal(
      app.document.querySelector('[aria-label="ADMM 最大迭代轮次"]'),
      null,
    );
    assert.equal(app.document.querySelector('#steps-select'), null);
    assert.equal(app.requests[0].region, 'SC'); // Existing request contract remains intact.
  } finally {
    await app.close();
  }
});

test('overview event uses the fixed grid while preserving other baseline parameters, events and result identity', async () => {
  const baseline = clusterFixture();
  const app = await mountDashboard({
    hydrate: true,
    view: 'overview',
    result: baseline,
  });
  try {
    assert.equal(app.document.querySelector('#region-select'), null);
    assert.equal(
      app.document.querySelector(`${eventArea} select`).value,
      'YA_C',
    );
    await app.changeSelect(`${eventArea} select`, 'YA_B');
    assert.equal(app.document.querySelector('#steps-select'), null);
    assert.equal(app.requests.length, 0);
    assert.deepEqual((await app.exported()).body, baseline);
    await app.click('[aria-label="下一计划时刻"]');
    await app.clickButton('当前时刻注入光伏大发');

    assert.equal(app.requests.length, 1);
    const request = app.requests[0];
    assert.equal(request.steps, 96);
    assert.deepEqual(request.solver, {
      formulation: 'misocp',
      quality_policy: 'quality-first-v1',
    });
    assert.equal(request.admm_max_iterations, undefined);
    for (const field of [
      'region',
      'storage_enabled',
      'communication_loss_probability',
      'communication_max_delay_iterations',
      'communication_outage_region',
      'communication_outage_start_iteration',
      'communication_outage_end_iteration',
    ])
      assert.deepEqual(request[field], baseline.request[field], field);
    assert.deepEqual(request.events.slice(0, -1), baseline.request.events);
    const { event_id, ...event } = request.events.at(-1);
    assert.match(event_id, /^[0-9a-f-]{36}$/);
    assert.deepEqual(event, {
      event_type: 'pv_surge',
      target: 'YA_B',
      time_axis: 'clock_minute',
      start: 180,
      end: 360,
      magnitude: 1.35,
      label: '延安合成微网 B（YA_B）03:00 起光伏大发',
    });
    assert.match(
      app.document.querySelector('.run-feedback').textContent,
      /YA_B.*03:00.*光伏大发/,
    );
    for (const control of app.document.querySelectorAll(
      `${eventArea} select, ${eventArea} button`,
    )) {
      assert.equal(control.disabled, true);
    }
    assert.deepEqual((await app.exported()).body, baseline);

    const branch = structuredClone(baseline);
    branch.request = {
      ...baseline.request,
      ...request,
      solver: { ...baseline.request.solver, ...request.solver },
    };
    branch.metadata.steps = 96;
    branch.metadata.dt_hours = 0.25;
    await app.finish('succeeded', branch);
    assert.equal(
      app.document.querySelector(`${eventArea} select`).value,
      'YA_B',
    );
    assert.equal(app.document.querySelector('#steps-select'), null);
    assert.deepEqual((await app.exported()).body, branch);
    assert.match(
      app.document.querySelector('.run-feedback').textContent,
      /YA_B.*03:00.*光伏大发/,
    );
  } finally {
    await app.close();
  }
});

test('single microgrid labels the next simulation region but keeps event targets attached to the completed result', async () => {
  const baseline = structuredClone(first);
  baseline.request.region = 'YA_B';
  baseline.metadata.region = 'YA_B';
  const app = await mountDashboard({ hydrate: true, result: baseline });
  try {
    assert.equal(
      app.document.querySelector('label[for="region-select"]')?.textContent,
      '仿真区域',
    );
    assert.equal(app.document.querySelector(`${eventArea} select`), null);
    await app.changeSelect('#region-select', 'YA_C');
    assert.match(
      app.document.querySelector(eventArea).textContent,
      /延安合成微网 B/,
    );
    assert.equal(app.requests.length, 0);
    await app.clickButton('当前时刻注入风电大发');
    assert.equal(app.requests[0].region, 'YA_B');
    assert.equal(app.requests[0].events.at(-1).target, 'YA_B');
    await app.finish('failed');
    assert.deepEqual((await app.exported()).body, baseline);
  } finally {
    await app.close();
  }
});

for (const unavailable of ['no-result', 'offline', 'no-playback']) {
  test(`renewable target and buttons stay disabled with readable reason: ${unavailable}`, async () => {
    const baseline = clusterFixture();
    if (unavailable === 'no-playback') {
      baseline.cluster_timeseries = [];
      baseline.timeseries = [];
    }
    const app = await mountDashboard({
      view: 'overview',
      result: baseline,
      hydrate: unavailable !== 'no-result',
      health: unavailable === 'offline' ? 'offline' : 'ok',
    });
    try {
      const area = app.document.querySelector(eventArea);
      const controls = area.querySelectorAll('select, button');
      assert.equal(controls.length, 3);
      for (const control of controls) assert.equal(control.disabled, true);
      assert.match(
        area.textContent,
        unavailable === 'no-result'
          ? /请先运行仿真/
          : unavailable === 'offline'
            ? /服务未连接/
            : /没有可用的回放时刻/,
      );
      assert.equal(app.requests.length, 0);
    } finally {
      await app.close();
    }
  });
}

test('an overview wind event at the end of the day remains bounded by 24:00', async () => {
  const baseline = clusterFixture();
  baseline.cluster_timeseries[0].time_hour = 23;
  baseline.cluster_timeseries[1].time_hour = 23.75;
  const app = await mountDashboard({
    hydrate: true,
    view: 'overview',
    result: baseline,
  });
  try {
    await app.changeSelect(`${eventArea} select`, 'SC');
    await app.clickButton('当前时刻注入风电大发');
    const event = app.requests[0].events.at(-1);
    assert.equal(event.target, 'SC');
    assert.equal(event.event_type, 'wind_surge');
    assert.equal(event.start, 1380);
    assert.equal(event.end, 1440);
    await app.finish('failed');
  } finally {
    await app.close();
  }
});
