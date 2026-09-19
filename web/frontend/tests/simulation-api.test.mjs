import test from 'node:test';
import assert from 'node:assert/strict';
import { getHealth, waitForSimulation, SimulationCancelledError } from '../lib/simulation-api.ts';

const status = (state = 'running') => ({ simulation_id: 'job-1', state });

test('non-JSON and structured API failures preserve usable error details', async t => {
  t.mock.method(globalThis, 'fetch', async () => new Response('service unavailable', { status: 503 }));
  await assert.rejects(getHealth(), /503: service unavailable/);
  t.mock.method(globalThis, 'fetch', async () => new Response(JSON.stringify({ detail: [{ msg: 'invalid steps' }] }), { status: 422 }));
  await assert.rejects(getHealth(), /invalid steps/);
});

test('already-completed job fetches result without opening a status socket', async t => {
  t.mock.method(globalThis, 'fetch', async () => new Response(JSON.stringify({ marker: 'result' })));
  t.mock.method(globalThis, 'WebSocket', () => { throw new Error('must not create socket'); });
  assert.deepEqual(await waitForSimulation(status('succeeded'), () => {}), { marker: 'result' });
});

test('already-cancelled job has explicit cancellation semantics', async () => {
  await assert.rejects(waitForSimulation(status('cancelled'), () => {}), SimulationCancelledError);
});

test('malformed stream falls back to polling and closes the old socket', async t => {
  let closed = false;
  class BrokenSocket {
    constructor() { queueMicrotask(() => this.onmessage({ data: 'invalid JSON' })); }
    close() { closed = true; }
  }
  const originalSocket = globalThis.WebSocket;
  globalThis.WebSocket = BrokenSocket;
  t.after(() => { globalThis.WebSocket = originalSocket; });
  t.mock.method(globalThis, 'fetch', async url => new Response(JSON.stringify(
    String(url).endsWith('/result') ? { marker: 'finished' } : status('succeeded'),
  )));
  const states = [];
  assert.deepEqual(await waitForSimulation(status(), s => states.push(s.state), 1), { marker: 'finished' });
  assert.equal(closed, true); assert.deepEqual(states, ['running', 'succeeded']);
});

test('aborting a subscription closes socket but does not cancel server job', async t => {
  let closed = false;
  const controller = new AbortController();
  class QuietSocket { constructor() { queueMicrotask(() => controller.abort()); } close() { closed = true; } }
  const originalSocket = globalThis.WebSocket;
  globalThis.WebSocket = QuietSocket;
  t.after(() => { globalThis.WebSocket = originalSocket; });
  const calls = [];
  t.mock.method(globalThis, 'fetch', async url => { calls.push(url); return new Response('{}'); });
  await assert.rejects(waitForSimulation(status(), () => {}, 1, controller.signal), { name: 'AbortError' });
  assert.equal(closed, true); assert.deepEqual(calls, []);
});
