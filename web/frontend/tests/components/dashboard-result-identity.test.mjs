/** Real React DOM lifecycle tests in jsdom; charts/widgets and network are ports.
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

async function mountDashboard({
  hydrate = false,
  view = 'single_microgrid',
  result = first,
  health = 'ok',
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
          if (name === 'recharts') return new Proxy({}, { get: () => Icon });
          if (name === '@/components/ui/label')
            return {
              Label: ({ children, htmlFor }) =>
                React.createElement('label', { htmlFor }, children),
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
    getSimulation: async () => ({ simulation_id: idA, state: 'succeeded' }),
    getSimulationResult: async () => structuredClone(result),
    waitForSimulation: async (status, onStatus) => {
      onStatus({ ...status, stage_label: 'Synthetic job', total_stages: 6 });
      if (count === 1) return structuredClone(result);
      return new Promise((resolve, reject) => {
        pending = { resolve, reject };
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
      assert.equal(button.disabled, false);
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
      await app.click('.run-button');
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
      /8 时段\s*·\s*60 秒\s*·\s*储能启用\s*·\s*MISOCP/,
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
    assert.equal(
      request.solver.time_limit_seconds,
      first.request.solver.time_limit_seconds,
    );
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

const eventArea = '[aria-label="新能源事件注入"]';

test('cluster coordination declares all regions without a region picker or renewable event controls', async () => {
  const app = await mountDashboard({
    view: 'cluster_coordination',
    result: clusterFixture(),
  });
  try {
    assert.equal(app.document.querySelector('#region-select'), null);
    assert.equal(app.document.querySelector(eventArea), null);
    const text = app.document.querySelector('.config-body').textContent;
    assert.match(text, /协调范围/);
    for (const code of ['SC', 'YA_B', 'YA_C']) assert.ok(text.includes(code));
    assert.equal(text.includes('重点展示区域'), false);
    await app.click('.run-button');
    assert.equal(app.requests[0].scenario_type, 'cluster_coordination');
    assert.equal(app.requests[0].region, 'SC'); // Existing request contract remains intact.
  } finally {
    await app.close();
  }
});

test('overview chooses an event target immediately while preserving baseline parameters, events and result identity', async () => {
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
    await app.changeSelect('#steps-select', '24');
    assert.equal(app.requests.length, 0);
    assert.deepEqual((await app.exported()).body, baseline);
    await app.click('[aria-label="下一分钟记录"]');
    await app.clickButton('当前时刻注入光伏大发');

    assert.equal(app.requests.length, 1);
    const request = app.requests[0];
    for (const field of [
      'region',
      'steps',
      'storage_enabled',
      'solver',
      'admm_max_iterations',
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
      start: 1,
      end: 181,
      magnitude: 1.35,
      label: '延安合成微网 B（YA_B）00:01 起光伏大发',
    });
    assert.match(
      app.document.querySelector('.run-feedback').textContent,
      /YA_B.*00:01.*光伏大发/,
    );
    for (const control of app.document.querySelectorAll(
      `${eventArea} select, ${eventArea} button`,
    )) {
      assert.equal(control.disabled, true);
    }
    assert.deepEqual((await app.exported()).body, baseline);

    const branch = structuredClone(baseline);
    branch.request = request;
    await app.finish('succeeded', branch);
    assert.equal(
      app.document.querySelector(`${eventArea} select`).value,
      'YA_B',
    );
    assert.equal(app.document.querySelector('#steps-select').value, '24');
    assert.deepEqual((await app.exported()).body, branch);
    assert.match(
      app.document.querySelector('.run-feedback').textContent,
      /YA_B.*00:01.*光伏大发/,
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
