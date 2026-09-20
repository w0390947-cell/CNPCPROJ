import type { SimulationResult } from '@/lib/simulation-api';
import styles from './style.module.css';

type Node = SimulationResult['topology_nodes'][number];
type Edge = SimulationResult['topology_edges'][number];

/** Layout follows recorded connectivity, including historical five-bus results. */
export function SingleMicrogridCanvas({
  nodes,
  edges,
  region,
}: {
  nodes: Node[];
  edges: Edge[];
  region: string;
}) {
  if (!nodes.length)
    return (
      <div className={styles.empty}>运行仿真后展示 {region} 的实际网络拓扑</div>
    );
  const roots = nodes.filter(
    (node) => !edges.some((edge) => edge.target === node.id),
  );
  const depths = new Map(roots.map((node) => [node.id, 0]));
  for (let pass = 0; pass < nodes.length; pass++) {
    for (const edge of edges) {
      const source = depths.get(edge.source);
      if (source !== undefined && !depths.has(edge.target))
        depths.set(edge.target, source + 1);
    }
  }
  const levels = Array.from(
    new Set(nodes.map((node) => depths.get(node.id) ?? 0)),
  ).sort((a, b) => a - b);
  const widest = Math.max(
    ...levels.map(
      (level) =>
        nodes.filter((node) => (depths.get(node.id) ?? 0) === level).length,
    ),
  );
  const width = Math.max(520, widest * 150);
  const height = levels.length * 100 + 20;
  const positions = new Map<string, { x: number; y: number }>();
  for (const level of levels) {
    const row = nodes.filter((node) => (depths.get(node.id) ?? 0) === level);
    row.forEach((node, index) =>
      positions.set(node.id, {
        x: (width * (index + 0.5)) / row.length,
        y: 50 + level * 100,
      }),
    );
  }
  const labels: Record<string, string> = {
    pcc: 'PCC 受电',
    main: '主母线',
    wind: '风电接入',
    pv: '光伏接入',
    flex: '储能 / SVG',
  };
  return (
    <div className={styles.scroll}>
      {/* oxlint-disable jsx-a11y/prefer-tag-over-role -- Inline SVG exposes its diagram and title as one accessible image. */}
      <svg
        viewBox={`0 0 ${width} ${height}`}
        style={{ minWidth: width }}
        role="img"
        aria-label={`${region} 网络拓扑：${nodes.length} 个母线，${edges.length} 条支路`}
      >
        <title>{region} 仿真输入网络拓扑</title>
        {edges.map((edge) => {
          const from = positions.get(edge.source);
          const to = positions.get(edge.target);
          return from && to ? (
            <g key={edge.id}>
              <title>
                {edge.id}：{edge.source} → {edge.target}
              </title>
              <path
                d={`M ${from.x} ${from.y + 25} V ${(from.y + to.y) / 2} H ${to.x} V ${to.y - 25}`}
                className={styles.edge}
              />
            </g>
          ) : null;
        })}
        {nodes.map((node) => {
          const position = positions.get(node.id)!;
          return (
            <g
              key={node.id}
              transform={`translate(${position.x}, ${position.y})`}
              className={styles.node}
            >
              <title>{node.id}</title>
              <rect x="-65" y="-26" width="130" height="52" rx="10" />
              <text y="-4">{node.label}</text>
              <text y="15" className={styles.kind}>
                {labels[node.kind] ?? '网络母线'}
              </text>
            </g>
          );
        })}
      </svg>
      {/* oxlint-enable jsx-a11y/prefer-tag-over-role */}
    </div>
  );
}
