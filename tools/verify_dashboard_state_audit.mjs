/** Item 13: execute unmodified component callbacks with controlled hooks/I/O.
 * Not a browser or React renderer E2E test. JSX widgets and scheduling are stubs;
 * dashboard/presentation/API request-construction source is compiled unchanged.
 */
import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import vm from 'node:vm';
import { createRequire } from 'node:module';

const root = process.cwd();
const frontend = path.join(root, 'web/frontend');
const require = createRequire(path.join(frontend, 'package.json'));
const ts = require('typescript');
const output = path.resolve(process.argv[2]);
const baseline = JSON.parse(fs.readFileSync(process.argv[3], 'utf8'));
const nextResult = JSON.parse(fs.readFileSync(process.argv[4], 'utf8'));
assert.notDeepEqual(nextResult, baseline, 'Success control requires a distinct real result');
fs.mkdirSync(output, { recursive: false });

function harness(outcome) {
  let cursor = 0, pending = [], tree, currentUrl = 'http://localhost:3000/single-microgrid';
  const slots = [], requests = [], downloads = [], navigation = [], blobs = new Map();
  let finish, createdCount = 0;
  const changed = (a, b) => !a || !b || a.length !== b.length || a.some((v, i) => v !== b[i]);
  const hooks = {
    useState(initial) {
      const i = cursor++;
      slots[i] ??= { value: typeof initial === 'function' ? initial() : initial };
      return [slots[i].value, value => { slots[i].value = typeof value === 'function' ? value(slots[i].value) : value; }];
    },
    useRef(value) { const i = cursor++; slots[i] ??= { current: value }; return slots[i]; },
    useMemo(fn, deps) {
      const i = cursor++;
      if (!slots[i] || changed(slots[i].deps, deps)) slots[i] = { deps, value: fn() };
      return slots[i].value;
    },
    useCallback(fn, deps) { return hooks.useMemo(() => fn, deps); },
    useEffect(fn, deps) {
      const i = cursor++;
      if (!slots[i] || changed(slots[i].deps, deps)) { slots[i] = { deps }; pending.push(fn); }
    },
    useSyncExternalStore(_subscribe, get) { return get(); },
  };
  const jsx = (type, props) => ({ type, props });
  const cache = new Map();
  let api;
  const fakeWindow = {
    location: { get href() { return currentUrl; } },
    history: { replaceState(_state, _title, url) { currentUrl = String(url); navigation.push(currentUrl); } },
    setTimeout: () => 1, setInterval: () => 1, clearInterval: () => {},
  };
  class CaptureURL extends URL {
    static createObjectURL(blob) { const id = `blob:${blobs.size}`; blobs.set(id, blob); return id; }
    static revokeObjectURL() {}
  }
  const document = {
    createElement(tag) {
      assert.equal(tag, 'a');
      return { click() { downloads.push({ filename: this.download, blob: blobs.get(this.href) }); } };
    },
  };
  const fetch = async (url, init) => {
    if (String(url).endsWith('/api/simulations') && init?.method === 'POST') {
      const payload = JSON.parse(init.body);
      requests.push(payload);
      const id = ++createdCount === 1 ? 'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa' : 'bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb';
      return new Response(JSON.stringify({ simulation_id: id, state: 'running', name: payload.name }));
    }
    throw new Error(`Unexpected transport: ${url}`);
  };
  function load(file) {
    if (cache.has(file)) return cache.get(file);
    const source = fs.readFileSync(path.join(frontend, file), 'utf8');
    const compiled = ts.transpileModule(source, {
      compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022, jsx: ts.JsxEmit.ReactJSX },
    }).outputText;
    const exports = {};
    const module = { exports };
    const context = {
      exports, module, console, process: { env: {} }, URL: CaptureURL,
      Blob, Response, AbortController, AbortSignal, fetch, window: fakeWindow, document,
      crypto: { randomUUID: () => 'audit-event' }, setTimeout, clearTimeout,
      require(name) {
        if (name === 'react') return hooks;
        if (name === 'react/jsx-runtime') return { jsx, jsxs: jsx, Fragment: 'fragment' };
        if (name === 'next/navigation') return { useRouter: () => ({ push: url => { currentUrl = url; } }) };
        if (name === '@/lib/simulation-api') return api;
        if (name === '@/lib/simulation-presentation') return load('lib/simulation-presentation.ts');
        if (name.startsWith('@/components/ui/') || ['lucide-react', 'recharts'].includes(name)) {
          return new Proxy({}, { get: (_target, key) => String(key) });
        }
        throw new Error(`Unexpected import: ${name}`);
      },
    };
    vm.runInNewContext(compiled, context, { filename: file });
    cache.set(file, module.exports);
    return module.exports;
  }
  const actualApi = load('lib/simulation-api.ts');
  api = {
    ...actualApi,
    getHealth: async () => ({ status: 'ok' }),
    waitForSimulation: async (status, onStatus) => {
      onStatus({ ...status, stage_label: 'Controlled audit job', total_stages: 6 });
      if (createdCount === 1) return structuredClone(baseline);
      return new Promise((resolve, reject) => { finish = { resolve, reject }; });
    },
    cancelSimulation: async id => {
      finish.reject(new actualApi.SimulationCancelledError());
      return { simulation_id: id, state: 'cancelled' };
    },
  };
  const Dashboard = load('components/simulation-dashboard.tsx').default;
  function render() { cursor = 0; tree = Dashboard({ initialView: 'single_microgrid' }); return tree; }
  function all(node, predicate) {
    if (node == null || typeof node !== 'object') return [];
    if (Array.isArray(node)) return node.flatMap(v => all(v, predicate));
    return [...(predicate(node) ? [node] : []), ...all(node.props?.children, predicate)];
  }
  function button(className) {
    const matches = all(tree, n => n.props?.className?.split(' ').includes(className));
    assert.equal(matches.length, 1, className);
    assert.equal(matches[0].props.disabled, false);
    return matches[0];
  }
  async function settle() { for (let i = 0; i < 10; i++) await new Promise(resolve => setImmediate(resolve)); render(); }
  async function exportCurrent() {
    button('export-button').props.onClick();
    const item = downloads.at(-1);
    return { filename: item.filename, body: JSON.parse(await item.blob.text()), url: currentUrl };
  }
  return (async () => {
    render();
    for (const effect of pending.splice(0)) effect();
    await settle();
    button('run-button').props.onClick();
    await settle();
    const before = await exportCurrent();
    button('run-button').props.onClick();
    await settle();
    const during = await exportCurrent();
    if (outcome === 'cancelled') await button('run-button').props.onClick();
    else if (outcome === 'failed') finish.reject(new Error('Controlled worker failure'));
    else finish.resolve(structuredClone(nextResult));
    await settle();
    const after = await exportCurrent();
    assert.deepEqual(during.body, before.body);
    assert.deepEqual(after.body, outcome === 'succeeded' ? nextResult : baseline);
    const result = {
      test_kind: 'component callbacks with controlled hooks and I/O; not browser E2E',
      outcome, requests, navigation,
      before: { filename: before.filename, url: before.url },
      during: { filename: during.filename, url: during.url, body_equals_first: JSON.stringify(during.body) === JSON.stringify(before.body) },
      after: { filename: after.filename, url: after.url, body_equals_first: JSON.stringify(after.body) === JSON.stringify(before.body) },
      visible_result_origin: all(tree, n => n.props?.className === 'result-origin').map(n => n.props.children),
      error_visible: all(tree, n => n.props?.role === 'alert').length > 0,
    };
    fs.writeFileSync(path.join(output, `${outcome}_export.json`), JSON.stringify(after.body, null, 2));
    return result;
  })();
}

const results = [];
for (const outcome of ['cancelled', 'failed', 'succeeded']) results.push(await harness(outcome));
fs.writeFileSync(path.join(output, 'summary.json'), JSON.stringify(results, null, 2));
console.log(JSON.stringify(results, null, 2));
