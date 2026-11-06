import type { UnifiedExecution } from '../../shared/api/generated/single-execution';

type Network = UnifiedExecution['domain']['network'];
type Point = { x: number; y: number };

const corePositions: Record<string, Point> = {
  SC_PCC: { x: 660, y: 55 }, SC_MAIN: { x: 660, y: 175 },
  SC_WIND_COLLECTOR: { x: 220, y: 295 }, SC_FLEX: { x: 520, y: 295 },
  SC_SVG: { x: 740, y: 295 }, SC_PV_BUS: { x: 1040, y: 295 },
  SC_WT1: { x: 110, y: 415 }, SC_WT2: { x: 330, y: 415 },
  SC_PV1: { x: 930, y: 415 }, SC_PV2: { x: 1150, y: 415 },
};
const coreEdges = new Set([
  'SC_PCC>SC_MAIN', 'SC_MAIN>SC_WIND_COLLECTOR', 'SC_MAIN>SC_FLEX',
  'SC_MAIN>SC_SVG', 'SC_MAIN>SC_PV_BUS', 'SC_WIND_COLLECTOR>SC_WT1',
  'SC_WIND_COLLECTOR>SC_WT2', 'SC_PV_BUS>SC_PV1', 'SC_PV_BUS>SC_PV2',
]);

/** Lay out the declared branches, preserving the 8.2 example's illustrated arrangement. */
export function topologyLayout(net: Network) {
  const core = net.buses.length === 10 && net.branches.length === coreEdges.size &&
    net.buses.every((bus) => corePositions[bus] != null) &&
    net.branches.every((branch) => coreEdges.has(`${net.buses[branch.parent]}>${net.buses[branch.child]}`));
  if (core) return { points: net.buses.map((bus) => corePositions[bus]), width: 1260, height: 470 };

  const children = net.buses.map(() => [] as number[]);
  net.branches.forEach((branch) => children[branch.parent]?.push(branch.child));
  const points: Point[] = net.buses.map(() => ({ x: 0, y: 0 }));
  const seen = new Set<number>();
  let leaf = 0;
  let depthMax = 0;
  function place(index: number, depth: number): number {
    if (seen.has(index) || !children[index]) return 110 + leaf++ * 220;
    seen.add(index);
    depthMax = Math.max(depthMax, depth);
    const xs = children[index].map((child) => place(child, depth + 1));
    const x = xs.length ? (xs[0] + xs[xs.length - 1]) / 2 : 110 + leaf++ * 220;
    points[index] = { x, y: 55 + depth * 120 };
    return x;
  }
  place(net.slack, 0);
  net.buses.forEach((_, i) => { if (!seen.has(i)) place(i, 0); });
  return { points, width: Math.max(220, leaf * 220), height: 110 + depthMax * 120 };
}

export function topologyEdge(from: Point, to: Point) {
  const start = from.y + 42;
  const end = to.y - 42;
  const middle = (start + end) / 2;
  return `M${from.x},${start} V${middle} H${to.x} V${end}`;
}
