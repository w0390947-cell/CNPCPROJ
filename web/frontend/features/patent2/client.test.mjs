import assert from 'node:assert/strict';
import { test } from 'node:test';
import fs from 'node:fs';
import vm from 'node:vm';
import ts from 'typescript';

function compile(url, dependencies = {}) {
  const mod = { exports: {} };
  const code = ts.transpileModule(fs.readFileSync(url, 'utf8'), {
    compilerOptions: {
      module: ts.ModuleKind.CommonJS,
      target: ts.ScriptTarget.ES2022,
      esModuleInterop: true,
    },
  }).outputText;
  vm.runInNewContext(code, {
    module: mod,
    exports: mod.exports,
    Error,
    process: { env: {} },
    require: (name) => {
      if (dependencies[name]) return dependencies[name];
      if (name.endsWith('.json'))
        return JSON.parse(
          fs.readFileSync(
            new URL(`../../${name.slice(2)}`, import.meta.url),
            'utf8',
          ),
        );
      throw new Error(`unhandled ${name}`);
    },
  });
  return mod.exports;
}
const schema = compile(new URL('../../shared/api/schema.ts', import.meta.url));
const { parseResult, RecordError } = compile(
  new URL('./client.ts', import.meta.url),
  { '@/shared/api/schema': schema },
);

test('a missing contract or unrelated single-grid result cannot be presented as this example', () => {
  assert.throws(() => parseResult({}), RecordError);
  const single = JSON.parse(
    fs.readFileSync(
      new URL(
        '../../tests/fixtures/synthetic-single-result.json',
        import.meta.url,
      ),
      'utf8',
    ),
  );
  assert.throws(() => parseResult(single), RecordError);
});

test(
  'real archived result passes schema, while a changed identity or time scale is rejected',
  { skip: !process.env.PATENT2_ARCHIVE },
  () => {
    const real = JSON.parse(
      fs.readFileSync(process.env.PATENT2_ARCHIVE, 'utf8'),
    );
    assert.equal(
      parseResult(real).cluster_execution.status,
      real.cluster_execution.status,
    );
    const wrong = structuredClone(real);
    wrong.metadata.dt_hours = 6;
    assert.throws(() => parseResult(wrong), RecordError);
    const mismatch = structuredClone(real);
    mismatch.request.patent2_case =
      real.patent2_evidence.inputs.scenario === 'normal'
        ? 'low_energy'
        : 'normal';
    assert.throws(() => parseResult(mismatch), RecordError);
    const malformed = structuredClone(real);
    malformed.cluster_execution.safety.steps[0].after[0].energy_mwh = 'invalid';
    assert.throws(() => parseResult(malformed), RecordError);
  },
);
