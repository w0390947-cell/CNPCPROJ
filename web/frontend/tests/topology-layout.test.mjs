import { test } from 'node:test';
import assert from 'node:assert/strict';
import { topologyEdge, topologyLayout } from '../features/single-execution/topology-layout.ts';

test('8.2 network follows the reference hierarchy regardless of bus order', () => {
  const buses = ['SC_WT2', 'SC_SVG', 'SC_PCC', 'SC_PV2', 'SC_MAIN', 'SC_FLEX', 'SC_PV_BUS', 'SC_WT1', 'SC_WIND_COLLECTOR', 'SC_PV1'];
  const pairs = [['SC_PCC','SC_MAIN'], ['SC_MAIN','SC_WIND_COLLECTOR'], ['SC_MAIN','SC_FLEX'], ['SC_MAIN','SC_SVG'], ['SC_MAIN','SC_PV_BUS'], ['SC_WIND_COLLECTOR','SC_WT1'], ['SC_WIND_COLLECTOR','SC_WT2'], ['SC_PV_BUS','SC_PV1'], ['SC_PV_BUS','SC_PV2']];
  const net = { buses, slack: buses.indexOf('SC_PCC'), branches: pairs.map(([a,b]) => ({ parent: buses.indexOf(a), child: buses.indexOf(b) })) };
  const { points } = topologyLayout(net);
  const p = (bus) => points[buses.indexOf(bus)];
  assert.ok(p('SC_PCC').y < p('SC_MAIN').y);
  assert.deepEqual(['SC_WIND_COLLECTOR','SC_FLEX','SC_SVG','SC_PV_BUS'].map((bus) => p(bus).y), [295,295,295,295]);
  assert.ok(p('SC_WIND_COLLECTOR').x < p('SC_FLEX').x && p('SC_FLEX').x < p('SC_SVG').x && p('SC_SVG').x < p('SC_PV_BUS').x);
  assert.ok(p('SC_WT1').x < p('SC_WT2').x && p('SC_PV1').x < p('SC_PV2').x);
  for (const [a, b] of pairs) {
    assert.ok(p(a).y < p(b).y);
    assert.match(topologyEdge(p(a), p(b)), /^M\d+,\d+ V\d+ H\d+ V\d+$/);
  }
});

test('other radial networks use their actual parent-child connections', () => {
  const net = { buses: ['root', 'left', 'right', 'leaf'], slack: 0, branches: [{parent:0,child:1},{parent:0,child:2},{parent:1,child:3}] };
  const { points } = topologyLayout(net);
  assert.ok(points[0].y < points[1].y && points[1].y < points[3].y);
  assert.ok(points[1].x < points[2].x);
});
