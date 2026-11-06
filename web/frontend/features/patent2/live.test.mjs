import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import vm from 'node:vm';
import { createRequire } from 'node:module';
import test from 'node:test';
import ts from 'typescript';
const require = createRequire(import.meta.url);
function load(url) {
  const mod = { exports: {} };
  vm.runInNewContext(
    ts.transpileModule(fs.readFileSync(url, 'utf8'), {
      compilerOptions: {
        module: ts.ModuleKind.CommonJS,
        target: ts.ScriptTarget.ES2022,
      },
    }).outputText,
    {
      module: mod,
      exports: mod.exports,
      require: (n) => (n === 'react' ? require(n) : { api: {} }),
      setTimeout,
      clearTimeout,
      Error,
    },
  );
  return mod.exports;
}
const { appendEvent } = load(new URL('./live.ts', import.meta.url));
const { recoveryTasks } = load(new URL('./model.ts', import.meta.url));
const { validate } = load(
  new URL('../../shared/api/schema.ts', import.meta.url),
);
const schema = JSON.parse(
  fs.readFileSync(
    new URL(
      '../../shared/api/generated/cluster-live.schema.json',
      import.meta.url,
    ),
  ),
);

test(
  'a real persisted live journal reproduces its completed result',
  {
    skip: !process.env.PATENT2_JOURNAL || !process.env.PATENT2_ARCHIVE,
  },
  () => {
    const folder = process.env.PATENT2_JOURNAL;
    const { stream } = JSON.parse(
      fs.readFileSync(path.join(folder, 'live.json'), 'utf8'),
    );
    const directory = path.join(folder, 'live', stream);
    let state = { id: 'journal-check', sequence: 0, total: 0, error: '' };
    for (const file of fs
      .readdirSync(directory)
      .filter((f) => f.endsWith('.json'))
      .sort()) {
      const event = JSON.parse(
        fs.readFileSync(path.join(directory, file), 'utf8'),
      );
      validate(event, schema);
      state = appendEvent(state, event);
    }
    const execution = JSON.parse(
      fs.readFileSync(process.env.PATENT2_ARCHIVE, 'utf8'),
    ).cluster_execution;
    assert.equal(
      JSON.stringify(state.view.safety.steps),
      JSON.stringify(execution.safety.steps),
    );
    assert.equal(
      JSON.stringify(state.view.rolling_updates),
      JSON.stringify(execution.rolling_updates),
    );
    assert.equal(
      JSON.stringify(recoveryTasks(state.view)),
      JSON.stringify(recoveryTasks(execution)),
    );
    for (const region of execution.stages.find((s) => s.stage === 'minute')
      .regions) {
      const live = state.view.measurements.find(
        (r) => r.region === region.region,
      );
      assert.equal(JSON.stringify(live.p_mw), JSON.stringify(region.p_mw));
      assert.equal(JSON.stringify(live.q_mvar), JSON.stringify(region.q_mvar));
    }
  },
);
test('out-of-order cycles and mismatched predecessor states are rejected without mutating the display', () => {
  const initial = { p_mw: [0], q_mvar: [0], energy_mwh: 1 };
  const s = appendEvent(
    { id: 'A', sequence: 0, total: 0, error: '' },
    {
      kind: 'cluster_initial',
      sequence: 1,
      total: 2,
      policy: {},
      domain: {
        regions: [{ region: 'SC', initial, devices: { resources: [{}] } }],
      },
      tasks: [],
      observations: [],
    },
  );
  const cycle = {
    kind: 'cluster_cycle',
    sequence: 2,
    minute: 1,
    step: {
      minute: 0,
      before: [initial],
      after: [{ ...initial, energy_mwh: 1.1 }],
    },
    pcc: { SC: [1, 0.1, null, null] },
    tasks: [],
  };
  assert.throws(() => appendEvent(s, { ...cycle, sequence: 3 }), /序号不连续/);
  assert.throws(() => appendEvent(s, { ...cycle, minute: 2 }), /节点不连续/);
  assert.throws(
    () =>
      appendEvent(s, {
        ...cycle,
        step: { ...cycle.step, before: [{ ...initial, energy_mwh: 2 }] },
      }),
    /不衔接/,
  );
  assert.equal(s.view.safety.steps.length, 0);
  const next = appendEvent(s, cycle);
  assert.equal(next.view.measurements[0].p_target_mw[0], null);
  assert.equal(next.view.safety.steps[0].after[0].energy_mwh, 1.1);
  assert.equal(next.view.status, undefined);
});
test(
  'actual saved safety steps satisfy the live contract and retain all recorded states',
  { skip: !process.env.PATENT2_ARCHIVE },
  () => {
    const result = JSON.parse(
      fs.readFileSync(process.env.PATENT2_ARCHIVE, 'utf8'),
    );
    const e = result.cluster_execution;
    let s = { id: 'archive-check', sequence: 0, total: 0, error: '' };
    const study = {
      kind: 'cluster_study',
      sequence: 1,
      inputs: result.patent2_evidence.inputs,
    };
    validate(study, schema);
    s = appendEvent(s, study);
    assert.equal(s.inputs.scenario, result.patent2_evidence.inputs.scenario);
    const initial = {
      kind: 'cluster_initial',
      sequence: 2,
      total: 60,
      domain: e.safety.domain,
      policy: e.policy,
      observations: [],
      tasks: [],
    };
    validate(initial, schema);
    s = appendEvent(s, initial);
    const regions = e.stages.find((x) => x.stage === 'minute').regions;
    for (const step of e.safety.steps) {
      const pcc = Object.fromEntries(
        regions.map((r) => [
          r.region,
          [
            r.p_mw[step.minute],
            r.q_mvar[step.minute],
            r.p_target_samples_mw?.[step.minute] ??
              r.p_target_mw[step.minute] ??
              null,
            r.q_target_samples_mvar?.[step.minute] ??
              r.q_target_mvar[step.minute] ??
              null,
          ],
        ]),
      );
      const event = {
        kind: 'cluster_cycle',
        sequence: s.sequence + 1,
        minute: step.minute + 1,
        step,
        pcc,
        tasks: [],
      };
      validate(event, schema);
      s = appendEvent(s, event);
    }
    assert.equal(
      JSON.stringify(s.view.safety.steps),
      JSON.stringify(e.safety.steps),
    );
    assert.equal(s.view.status, undefined);
  },
);
