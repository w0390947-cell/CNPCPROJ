import test from 'node:test';
import assert from 'node:assert/strict';
import {
  getHealth,
  createSimulation,
  waitForSimulation,
  SimulationCancelledError,
} from '../lib/simulation-api.ts';

const status = (state = 'running') => ({ simulation_id: 'job-1', state });

test('new cluster tasks normalize old grids without changing other scenarios or the source configuration', async (t) => {
  const requests = [];
  t.mock.method(globalThis, 'fetch', async (_url, options) => {
    requests.push(JSON.parse(options.body));
    return new Response(JSON.stringify(status('queued')));
  });
  for (const scenario of [
    'cluster_coordination',
    'single_microgrid',
    'communication_fault',
    'group_control',
  ]) {
    for (const steps of [4, 8, 24, 96, 48]) {
      const configuration = Object.freeze({
        region: 'SC',
        steps,
        storageEnabled: true,
        timeLimitSeconds: 30,
        admmMaxIterations: 100,
      });
      await createSimulation(scenario, configuration);
      const request = requests.at(-1);
      const expected = scenario === 'cluster_coordination' ? 96 : steps;
      assert.equal(request.steps, expected);
      assert.equal(request.name, `SC ${expected}点${scenario}网页仿真`);
      assert.equal(configuration.steps, steps);
      if (
        scenario === 'cluster_coordination' ||
        scenario === 'single_microgrid'
      ) {
        assert.equal(request.solver.quality_policy, 'quality-first-v1');
        assert.equal(request.solver.time_limit_seconds, undefined);
        assert.equal(request.admm_max_iterations, undefined);
      } else {
        assert.equal(request.solver.time_limit_seconds, 30);
        assert.equal(request.admm_max_iterations, 100);
        assert.equal(request.solver.quality_policy, undefined);
      }
    }
  }
});

for (const transport of ['stream', 'poll']) {
  test(`${transport} transports restored rolling counts and terminal evidence unchanged`, async (t) => {
    const initial = {
      ...status(),
      rolling: { current_window: 2, completed_windows: 1, total_windows: 3 },
    };
    const update = {
      ...initial,
      rolling: { ...initial.rolling, completed_windows: 2 },
    };
    const terminal = { ...update, state: 'succeeded', result_available: true };
    class Socket {
      constructor() {
        queueMicrotask(() => {
          if (transport === 'poll') this.onerror();
          else {
            this.onmessage({ data: JSON.stringify(update) });
            this.onmessage({ data: JSON.stringify(terminal) });
          }
        });
      }
      close() {}
    }
    const originalSocket = globalThis.WebSocket;
    globalThis.WebSocket = Socket;
    t.after(() => {
      globalThis.WebSocket = originalSocket;
    });
    const queued = [update, terminal];
    t.mock.method(
      globalThis,
      'fetch',
      async (url) =>
        new Response(
          JSON.stringify(
            String(url).endsWith('/result')
              ? { passed: false }
              : queued.shift(),
          ),
        ),
    );
    const received = [];
    assert.deepEqual(
      await waitForSimulation(initial, (s) => received.push(s), 1),
      { passed: false },
    );
    assert.deepEqual(received, [initial, update, terminal]);
  });
}

test('non-JSON and structured API failures preserve usable error details', async (t) => {
  t.mock.method(
    globalThis,
    'fetch',
    async () => new Response('service unavailable', { status: 503 }),
  );
  await assert.rejects(getHealth(), /503: service unavailable/);
  t.mock.method(
    globalThis,
    'fetch',
    async () =>
      new Response(JSON.stringify({ detail: [{ msg: 'invalid steps' }] }), {
        status: 422,
      }),
  );
  await assert.rejects(getHealth(), /invalid steps/);
});

test('already-completed job fetches result without opening a status socket', async (t) => {
  t.mock.method(
    globalThis,
    'fetch',
    async () => new Response(JSON.stringify({ marker: 'result' })),
  );
  t.mock.method(globalThis, 'WebSocket', () => {
    throw new Error('must not create socket');
  });
  assert.deepEqual(await waitForSimulation(status('succeeded'), () => {}), {
    marker: 'result',
  });
});

test('already-cancelled job has explicit cancellation semantics', async () => {
  await assert.rejects(
    waitForSimulation(status('cancelled'), () => {}),
    SimulationCancelledError,
  );
});

test('malformed stream falls back to polling and closes the old socket', async (t) => {
  let closed = false;
  class BrokenSocket {
    constructor() {
      queueMicrotask(() => this.onmessage({ data: 'invalid JSON' }));
    }
    close() {
      closed = true;
    }
  }
  const originalSocket = globalThis.WebSocket;
  globalThis.WebSocket = BrokenSocket;
  t.after(() => {
    globalThis.WebSocket = originalSocket;
  });
  t.mock.method(
    globalThis,
    'fetch',
    async (url) =>
      new Response(
        JSON.stringify(
          String(url).endsWith('/result')
            ? { marker: 'finished' }
            : status('succeeded'),
        ),
      ),
  );
  const states = [];
  assert.deepEqual(
    await waitForSimulation(status(), (s) => states.push(s.state), 1),
    { marker: 'finished' },
  );
  assert.equal(closed, true);
  assert.deepEqual(states, ['running', 'succeeded']);
});

test('aborting a subscription closes socket but does not cancel server job', async (t) => {
  let closed = false;
  const controller = new AbortController();
  class QuietSocket {
    constructor() {
      queueMicrotask(() => controller.abort());
    }
    close() {
      closed = true;
    }
  }
  const originalSocket = globalThis.WebSocket;
  globalThis.WebSocket = QuietSocket;
  t.after(() => {
    globalThis.WebSocket = originalSocket;
  });
  const calls = [];
  t.mock.method(globalThis, 'fetch', async (url) => {
    calls.push(url);
    return new Response('{}');
  });
  await assert.rejects(
    waitForSimulation(status(), () => {}, 1, controller.signal),
    { name: 'AbortError' },
  );
  assert.equal(closed, true);
  assert.deepEqual(calls, []);
});
