/** React DOM interaction checks. These do not claim browser layout coverage. */
import assert from 'node:assert/strict';
import { test } from 'node:test';
import fs from 'node:fs';
import path from 'node:path';
import vm from 'node:vm';
import { createRequire } from 'node:module';
import { fileURLToPath } from 'node:url';
import { JSDOM } from 'jsdom';
import React, { act } from 'react';
import { createRoot } from 'react-dom/client';
import ts from 'typescript';

const root = fileURLToPath(new URL('../../', import.meta.url)),
  require = createRequire(import.meta.url);
const input = (scenario = 'normal') => ({
  scenario,
  version: 'patent2-example-v1',
  regions: [],
  cluster_limit_mw: 26,
  price_cny_per_mwh: [280, 280, 280, 280],
  input_sha256: 'test-only',
  input_snapshot_json: '{}',
});
const fixture = (scenario = 'normal') => ({
  metadata: { generated_at: '2026-11-05T00:00:00Z' },
  admm_history: [],
  patent2_evidence: {
    inputs: input(scenario),
    replayed_minutes: 2,
    code_sha256: 'test-only',
  },
  cluster_execution: {
    status: 'unknown',
    policy: { horizon_minutes: 240 },
    stages: [],
    rolling_updates: [],
    reserve_recovery_history: [],
    reserve_observations: [],
    safety: {
      domain: { regions: [] },
      steps: [0, 1].map((minute) => ({
        minute,
        source: minute ? 'backup' : 'economic',
        before: [],
        after: [],
        commands: [],
        path: { networks: [] },
      })),
    },
  },
});

async function mount(
  overrides = {},
  url = 'http://localhost/patent2',
  realEvidence = false,
) {
  const dom = new JSDOM('<div id="root"></div>', { url }),
    prior = {
      window: globalThis.window,
      document: globalThis.document,
      act: globalThis.IS_REACT_ACT_ENVIRONMENT,
    };
  globalThis.window = dom.window;
  globalThis.document = dom.window.document;
  globalThis.IS_REACT_ACT_ENVIRONMENT = true;
  const calls = [],
    cache = new Map(),
    exports = [];
  const api = {
    live: async () => ({ stream: null, cursor: 0, more: false, events: [] }),
    inputs: async (s) => input(s),
    library: async () => ['normal', 'low_energy'],
    result: async (id) =>
      fixture(id.includes('low_energy') ? 'low_energy' : 'normal'),
    run: async (scenario) => {
      calls.push(scenario);
      return { simulation_id: 'new', state: 'running' };
    },
    status: async () => ({ state: 'succeeded', simulation_id: 'new' }),
    cancel: async () => ({ state: 'cancelled' }),
    ...overrides,
  };
  function load(file) {
    const full = path.join(root, file);
    if (cache.has(full)) return cache.get(full);
    if (file.endsWith('.json'))
      return JSON.parse(fs.readFileSync(full, 'utf8'));
    const compiled = ts.transpileModule(fs.readFileSync(full, 'utf8'), {
      compilerOptions: {
        module: ts.ModuleKind.CommonJS,
        target: ts.ScriptTarget.ES2022,
        jsx: ts.JsxEmit.ReactJSX,
      },
    }).outputText;
    const mod = { exports: {} };
    vm.runInNewContext(
      compiled,
      {
        module: mod,
        exports: mod.exports,
        console,
        Error,
        process: { env: {} },
        window: dom.window,
        document: dom.window.document,
        localStorage: dom.window.localStorage,
        URL: class extends URL {
          static createObjectURL(blob) {
            exports.push(blob);
            return 'blob:test';
          }
          static revokeObjectURL() {}
        },
        URLSearchParams,
        Blob,
        TextEncoder,
        crypto: globalThis.crypto,
        AbortSignal,
        fetch,
        setTimeout,
        clearTimeout,
        setInterval,
        clearInterval,
        require(name) {
          if (name === 'react' || name === 'react/jsx-runtime')
            return require(name);
          if (name === 'next/navigation')
            return { useRouter: () => ({ push() {} }) };
          if (name === 'lucide-react')
            return new Proxy({}, { get: () => () => null });
          if (name === '@/features/cluster-coordination')
            return realEvidence
              ? load('features/cluster-coordination/execution-panel.tsx')
              : {
                  ClusterExecutionPanel: () =>
                    React.createElement('div', null, 'existing evidence panel'),
                };
          if (name.endsWith('.css'))
            return { default: new Proxy({}, { get: (_, k) => k }) };
          if (name === './client')
            return {
              ...(realEvidence
                ? load('features/patent2/client.ts')
                : {
                    HttpError: class extends Error {},
                    RecordError: class extends Error {},
                  }),
              api,
            };
          const relative = name.startsWith('@/')
            ? name.slice(2)
            : path.join(path.dirname(file), name);
          const found = ['', '.ts', '.tsx', '/index.ts']
            .map((ext) => relative + ext)
            .find(
              (p) =>
                fs.existsSync(path.join(root, p)) &&
                fs.statSync(path.join(root, p)).isFile(),
            );
          if (found)
            return found.endsWith('.json')
              ? { default: load(found) }
              : load(found);
          throw new Error(`unhandled import ${name}`);
        },
      },
      { filename: full },
    );
    cache.set(full, mod.exports);
    return mod.exports;
  }
  dom.window.HTMLAnchorElement.prototype.click = function () {};
  const Page = load('features/patent2/ui.tsx').default,
    reactRoot = createRoot(dom.window.document.getElementById('root'));
  await act(async () => {
    reactRoot.render(React.createElement(Page));
  });
  const doc = dom.window.document;
  return {
    doc,
    api,
    calls,
    exports,
    async wait(ms = 600) {
      await act(async () => {
        await new Promise((resolve) => setTimeout(resolve, ms));
      });
    },
    async click(label) {
      const b = [...doc.querySelectorAll('button')].find(
        (b) =>
          b.textContent.trim() === label ||
          b.getAttribute('aria-label') === label,
      );
      assert.ok(b, `button ${label}`);
      await act(async () => b.click());
    },
    async record(value) {
      await act(async () => {
        const s = doc.querySelector('[aria-label="选择已保存的真实运行记录"]');
        s.value = value;
        s.dispatchEvent(new dom.window.Event('change', { bubbles: true }));
      });
    },
    async close() {
      await act(async () => reactRoot.unmount());
      globalThis.window = prior.window;
      globalThis.document = prior.document;
      globalThis.IS_REACT_ACT_ENVIRONMENT = prior.act;
      dom.window.close();
    },
  };
}

test('service indicator waits for a response before reporting connection success or failure', async () => {
  for (const connected of [true, false]) {
    let resolve, reject;
    const response = new Promise((yes, no) => {
      resolve = yes;
      reject = no;
    });
    const app = await mount({
      library: () => response.then(() => ['normal', 'low_energy']),
      inputs: () => response.then(() => input()),
    });
    try {
      const indicator = () => app.doc.querySelector('.system-ready');
      assert.equal(indicator().textContent, '正在连接仿真服务');
      assert.ok(indicator().classList.contains('checking'));
      await act(async () => {
        if (connected) resolve();
        else reject(new Error('service unavailable'));
      });
      assert.equal(
        indicator().textContent,
        connected ? '仿真服务已连接' : '仿真服务未连接',
      );
      assert.ok(indicator().classList.contains(connected ? 'online' : 'offline'));
    } finally {
      await app.close();
    }
  }
});

test('live execution preserves the selected node and chapter when the final record arrives', async () => {
  const result = fixture();
  const e = result.cluster_execution;
  let completed = false,
    available = 1;
  const events = [
    {
      kind: 'cluster_initial',
      sequence: 1,
      total: 2,
      domain: e.safety.domain,
      policy: e.policy,
      tasks: [],
      observations: [],
    },
    ...e.safety.steps.map((step, i) => ({
      kind: 'cluster_cycle',
      sequence: i + 2,
      minute: i + 1,
      step,
      pcc: {},
      tasks: [],
    })),
  ];
  const app = await mount({
    status: async () => ({
      simulation_id: 'new',
      state: completed ? 'succeeded' : 'running',
    }),
    result: async () => result,
    live: async (_id, after) => ({
      stream: 'one',
      cursor: available + 1,
      more: false,
      events: events.slice(after, available + 1),
    }),
  });
  try {
    await app.click('现场重新计算');
    assert.ok(app.doc.querySelector('[aria-label="实时执行摘要"]'));
    assert.equal(
      app.doc.querySelector('[aria-label="完成节点分钟"]').value,
      '1',
    );
    await app.click('上一分钟');
    await app.click('04专项任务');
    available = 2;
    await app.wait();
    assert.equal(
      app.doc.querySelector('[aria-label="完成节点分钟"]').value,
      '0',
    );
    assert.ok(app.doc.querySelector('[aria-label="恢复任务当前状态"]'));
    completed = true;
    await app.wait(2200);
    assert.equal(
      app.doc.querySelector('[aria-label="完成节点分钟"]').value,
      '0',
    );
    assert.equal(app.doc.querySelector('[aria-label="实时执行摘要"]'), null);
    assert.ok(app.doc.querySelector('[aria-label="恢复任务最终状态"]'));
    assert.match(app.doc.body.textContent, /未获完整证明/);
    await app.click('跟随最新节点');
    assert.equal(
      app.doc.querySelector('[aria-label="完成节点分钟"]').value,
      '2',
    );
  } finally {
    await app.close();
  }
});

test('cancelled live task retains committed nodes and never promotes the previous result', async () => {
  const e = fixture().cluster_execution;
  let cancelled = false;
  const app = await mount({
    status: async () => ({
      simulation_id: 'new',
      state: cancelled ? 'cancelled' : 'running',
    }),
    cancel: async () => {
      cancelled = true;
      return { simulation_id: 'new', state: 'cancelled' };
    },
    live: async (_id, after) => ({
      stream: 'new-stream',
      more: false,
      cursor: 2,
      events: [
        {
          kind: 'cluster_initial',
          sequence: 1,
          total: 2,
          domain: e.safety.domain,
          policy: e.policy,
          tasks: [],
          observations: [],
        },
        {
          kind: 'cluster_cycle',
          sequence: 2,
          minute: 1,
          step: e.safety.steps[0],
          pcc: {},
          tasks: [],
        },
      ].slice(after),
    }),
  });
  try {
    await app.record('library:normal');
    await app.click('现场重新计算');
    assert.equal(app.doc.querySelector('[aria-label="完成节点分钟"]').max, '1');
    await app.click('取消计算');
    await app.wait(2200);
    assert.equal(app.doc.querySelector('[aria-label="完成节点分钟"]').max, '1');
    await app.click('05证据与结论');
    assert.match(
      app.doc.body.textContent,
      /整体安全链复验、经济性和最终评价将在完整结果生成后展示/,
    );
  } finally {
    await app.close();
  }
});

test('a transient live read failure keeps committed data and resumes by sequence', async () => {
  const e = fixture().cluster_execution;
  let fail = false,
    available = 1;
  const cursors = [];
  const events = [
    {
      kind: 'cluster_initial',
      sequence: 1,
      total: 2,
      domain: e.safety.domain,
      policy: e.policy,
      tasks: [],
      observations: [],
    },
    ...e.safety.steps.map((step, i) => ({
      kind: 'cluster_cycle',
      sequence: i + 2,
      minute: i + 1,
      step,
      pcc: {},
      tasks: [],
    })),
  ];
  const app = await mount({
    status: async () => ({ simulation_id: 'new', state: 'running' }),
    live: async (_id, after) => {
      cursors.push(after);
      if (fail) throw Error('temporary disconnect');
      return {
        stream: 'same',
        cursor: available + 1,
        more: false,
        events: events.slice(after, available + 1),
      };
    },
  });
  try {
    await app.click('现场重新计算');
    fail = true;
    await app.wait();
    assert.equal(app.doc.querySelector('[aria-label="完成节点分钟"]').max, '1');
    assert.match(app.doc.body.textContent, /保留已收到节点/);
    fail = false;
    available = 2;
    await app.wait(1700);
    assert.equal(app.doc.querySelector('[aria-label="完成节点分钟"]').max, '2');
    assert.equal(cursors.at(-1), 2);
    assert.doesNotMatch(app.doc.body.textContent, /temporary disconnect/);
  } finally {
    await app.close();
  }
});

test('restoring an active low-energy job uses its recorded conditions and follows completed nodes', async () => {
  const e = fixture('low_energy').cluster_execution;
  const events = [
    { kind: 'cluster_study', sequence: 1, inputs: input('low_energy') },
    {
      kind: 'cluster_initial',
      sequence: 2,
      total: 2,
      domain: e.safety.domain,
      policy: e.policy,
      tasks: [],
      observations: [],
    },
    {
      kind: 'cluster_cycle',
      sequence: 3,
      minute: 1,
      step: e.safety.steps[0],
      pcc: {},
      tasks: [],
    },
  ];
  const app = await mount(
    {
      status: async () => ({ simulation_id: 'restored-low', state: 'running' }),
      live: async (_id, after) => ({
        stream: 'low',
        cursor: 3,
        more: false,
        events: events.slice(after),
      }),
    },
    'http://localhost/patent2?simulation=restored-low',
  );
  try {
    assert.equal(app.calls.length, 0);
    const selected = app.doc.querySelector('[aria-pressed="true"]');
    assert.match(selected.textContent, /低电量恢复/);
    assert.equal(
      app.doc.querySelector('[aria-label="完成节点分钟"]').value,
      '1',
    );
    assert.match(app.doc.body.textContent, /低电量恢复/);
  } finally {
    await app.close();
  }
});

test('opening page does not compute; saved results remain unknown and completion nodes are playable', async () => {
  const app = await mount();
  try {
    assert.equal(app.calls.length, 0);
    assert.deepEqual([...app.doc.querySelectorAll('nav[aria-label="主导航"] button')].map(b => b.textContent.trim()), ['新能源最优协同控制算法仿真', '多层级微电网集群控制算法仿真']);
    assert.equal(app.doc.querySelector('input[type=file]'), null);
    assert.doesNotMatch(
      app.doc.body.textContent,
      /导入回放文件|离线导入|本地回放可用/,
    );
    assert.equal(app.doc.querySelector('[aria-label="执行记录播放器"]'), null);
    await app.record('library:normal');
    assert.match(app.doc.body.textContent, /未获完整证明/);
    await app.click('02规划决策');
    assert.match(app.doc.body.textContent, /滚动规划：用真实初值更新/);
    await app.click('03执行监控');
    await app.click('下一分钟');
    assert.match(app.doc.body.textContent, /1 min 完成节点/);
    assert.match(app.doc.body.textContent, /0→1 min 区间/);
    await app.click('下一分钟');
    assert.match(app.doc.body.textContent, /认证后备/);
    await app.click('04专项任务');
    assert.match(app.doc.body.textContent, /该完成节点没有备用观测记录/);
    assert.equal(app.calls.length, 0);
    await app.click('导出完整记录');
    assert.equal(
      JSON.parse(await app.exports[0].text()).cluster_execution.status,
      'unknown',
    );
    await app.record('library:low_energy');
    assert.match(app.doc.body.textContent, /library:low_energy/);
    await app.click('正常初始电量');
    assert.match(app.doc.body.textContent, /library:normal/);
  } finally {
    await app.close();
  }
});

test('user run owns result identity and invokes only the selected fixed scenario', async () => {
  const app = await mount();
  try {
    await app.click('低电量恢复');
    await app.click('现场重新计算');
    assert.deepEqual(app.calls, ['low_energy']);
    assert.match(app.doc.body.textContent, /服务器真实运行 · new/);
    assert.match(app.doc.body.textContent, /未获完整证明/);
  } finally {
    await app.close();
  }
});

test('a late URL restoration cannot overwrite a newly requested run', async () => {
  let finish;
  const old = new Promise((resolve) => {
    finish = resolve;
  });
  const app = await mount(
    {
      result: (id) =>
        id === 'library:normal' ? old : Promise.resolve(fixture('low_energy')),
    },
    'http://localhost/patent2?simulation=library:normal',
  );
  try {
    await app.click('低电量恢复');
    await app.click('现场重新计算');
    await act(async () => {
      finish(fixture());
      await old;
    });
    assert.match(app.doc.body.textContent, /服务器真实运行 · new/);
    assert.doesNotMatch(
      app.doc.body.textContent,
      /服务器保存记录 · library:normal/,
    );
  } finally {
    await app.close();
  }
});

test('failed job is reported without inventing a completed simulation', async () => {
  const app = await mount({
    status: async () => ({
      state: 'failed',
      error_message: 'test solver failure',
    }),
  });
  try {
    await app.click('现场重新计算');
    assert.match(
      app.doc.querySelector('[role="alert"]').textContent,
      /test solver failure/,
    );
    assert.equal(app.doc.querySelector('[aria-label="执行记录播放器"]'), null);
  } finally {
    await app.close();
  }
});

test('saved-record options contain only demo library entries before and after a new run', async () => {
  let historyCalls = 0,
    libraryCalls = 0;
  const app = await mount({
    history: async () => {
      historyCalls++;
      return [{ simulation_id: 'recent', name: '近期计算' }];
    },
    library: async () => {
      libraryCalls++;
      return ['normal', 'low_energy'];
    },
  });
  const options = () =>
    [
      ...app.doc.querySelectorAll(
        '[aria-label="选择已保存的真实运行记录"] option',
      ),
    ].map((o) => o.value);
  try {
    assert.deepEqual(options(), ['', 'library:normal', 'library:low_energy']);
    await app.click('刷新保存记录');
    assert.equal(libraryCalls, 2);
    await app.click('现场重新计算');
    assert.match(app.doc.body.textContent, /服务器真实运行 · new/);
    assert.deepEqual(options(), ['', 'library:normal', 'library:low_energy']);
    assert.equal(historyCalls, 0);
    assert.doesNotMatch(app.doc.body.textContent, /近期现场计算/);
    await app.record('library:low_energy');
    assert.match(
      app.doc.body.textContent,
      /服务器保存记录 · library:low_energy/,
    );
  } finally {
    await app.close();
  }
});

test('a missing demo library does not populate the selector with recent tasks', async () => {
  const app = await mount({ library: async () => [] });
  try {
    const select = app.doc.querySelector(
      '[aria-label="选择已保存的真实运行记录"]',
    );
    assert.equal(select.options.length, 1);
    assert.match(select.textContent, /暂无已保存的示范计算/);
    assert.equal(app.calls.length, 0);
  } finally {
    await app.close();
  }
});

test('place names in progress and errors are displayed as region codes', async () => {
  const app = await mount({
    status: async () => ({
      simulation_id: 'new',
      state: 'failed',
      stage_label: 'Shancheng 校核',
      error_message: '化子坪与榆树校核未完成',
    }),
  });
  try {
    await app.click('现场重新计算');
    assert.match(app.doc.body.textContent, /SC 校核/);
    assert.match(
      app.doc.querySelector('[role="alert"]').textContent,
      /YA_B与YA_C校核未完成/,
    );
    assert.doesNotMatch(
      app.doc.body.textContent,
      /山城|化子坪|榆树|shancheng/i,
    );
  } finally {
    await app.close();
  }
});

test(
  'actual archived results render every presentation tab',
  { skip: !process.env.PATENT2_ARCHIVE },
  async () => {
    const result = JSON.parse(
      fs.readFileSync(process.env.PATENT2_ARCHIVE, 'utf8'),
    );
    const app = await mount(
      { result: async () => result },
      'http://localhost/patent2',
      true,
    );
    try {
      await app.record(`library:${result.patent2_evidence.inputs.scenario}`);
      const curves = () => app.doc.querySelector('[aria-label="外部输入与执行联动"]');
      assert.equal(curves().querySelectorAll('figure').length, 4);
      await app.click('下一分钟');
      assert.match(curves().textContent, /0→1 min 执行区间/);
      assert.ok(curves().querySelector('[data-series="observed"]').getAttribute('d'));
      assert.equal(curves().querySelector('[aria-label="风电可用功率输入曲线"] [data-series="declared-range"]'), null);
      await act(async () => {
        const region = curves().querySelector('select');
        region.value = 'YA_B';
        region.dispatchEvent(new window.Event('change', { bubbles: true }));
      });
      // The input chart keyboard also uses the page's completion-node player.
      for (let i = 1; i < 15; i++) await app.click('下一分钟');
      assert.match(curves().textContent, /YA_B · 当前输入对应 14→15/);
      await act(async () => curves().querySelector('button[aria-label^="负荷有功 P输入曲线"]').dispatchEvent(new window.KeyboardEvent('keydown', { key: 'ArrowLeft', bubbles: true })));
      assert.equal(app.doc.querySelector('[aria-label="完成节点分钟"]').value, '14');
      await app.click('下一分钟');
      await app.click('查看对应规划');
      assert.match(app.doc.body.textContent, /选中窗口 0—15/);
      await app.click('03执行监控');
      assert.match(curves().textContent, /14→15 min 执行区间/);
      for (const label of [
        '01共同条件',
        '02规划决策',
        '03执行监控',
        '04专项任务',
        '05证据与结论',
      ]) {
        await app.click(label);
        assert.doesNotMatch(app.doc.body.textContent, /不是现场实测|非现场|并非现场|待现场|仿真观测|模拟执行|模拟运行|not field-validated/i);
        assert.doesNotMatch(
          app.doc.body.textContent,
          /山城|化子坪|榆树|延安|shancheng|huaziping|yushu/i,
        );
        for (const element of app.doc.querySelectorAll(
          '[title], [aria-label]',
        )) {
          assert.doesNotMatch(
            `${element.getAttribute('title')} ${element.getAttribute('aria-label')}`,
            /山城|化子坪|榆树|shancheng|huaziping|yushu/i,
          );
        }
        if (label === '04专项任务') {
          const rows = app.doc.querySelectorAll(
            '[aria-label="恢复任务最终状态"] tbody tr',
          );
          assert.equal(rows.length, 6);
          for (const row of rows) assert.match(row.textContent, /已恢复/);
        }
      }
      assert.match(app.doc.body.textContent, /60 \/ 60/);
      assert.ok(app.doc.querySelector('[aria-label="集群执行校核"]'));
    } finally {
      await app.close();
    }
  },
);

test(
  'server history remains playable and exports preserve the complete record and input snapshot',
  { skip: !process.env.PATENT2_ARCHIVE },
  async () => {
    const result = JSON.parse(
      fs.readFileSync(process.env.PATENT2_ARCHIVE, 'utf8'),
    );
    const app = await mount(
      { result: async () => result },
      'http://localhost/patent2',
      true,
    );
    try {
      await app.record(`library:${result.patent2_evidence.inputs.scenario}`);
      assert.match(app.doc.body.textContent, /服务器保存记录/);
      assert.equal(app.doc.querySelector('input[type=file]'), null);
      assert.doesNotMatch(app.doc.body.textContent, /导入回放文件/);
      await app.click('导出完整记录');
      assert.equal(await app.exports[0].text(), JSON.stringify(result));
      await app.click('下载原始输入快照');
      assert.equal(
        await app.exports[1].text(),
        result.patent2_evidence.inputs.input_snapshot_json,
      );
      assert.equal(app.doc.querySelector('[role="alert"]'), null);
      await app.click('03执行监控');
      await app.click('下一分钟');
      assert.match(app.doc.body.textContent, /1 min 完成节点/);
      assert.equal(app.calls.length, 0);
    } finally {
      await app.close();
    }
  },
);
