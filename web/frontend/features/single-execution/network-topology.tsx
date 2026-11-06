import type {
  TubeState,
  UnifiedExecution,
} from '../../shared/api/generated/single-execution';
import { interval, value } from './presentation';
import { topologyEdge, topologyLayout } from './topology-layout';
import styles from './live.module.css';

export function NetworkTopology({
  network,
  resources,
  state,
  voltage,
  animated = false,
  label = '实时网络节点与接线',
}: {
  network: UnifiedExecution['domain']['network'];
  resources: UnifiedExecution['domain']['devices']['resources'];
  state: TubeState;
  voltage?: Array<{ low: number; high: number }> | null;
  animated?: boolean;
  label?: string;
}) {
  const layout = topologyLayout(network);
  return (
    <div className={styles.network}>
      <h3>网络节点状态</h3>
      <svg viewBox={`0 0 ${layout.width} ${layout.height}`} aria-label={label}>
        {network.branches.map((b, i) => (
          <path
            key={i}
            data-parent={network.buses[b.parent]}
            data-child={network.buses[b.child]}
            className={animated ? styles.line : ''}
            d={topologyEdge(layout.points[b.parent], layout.points[b.child])}
            stroke="var(--plot-blue, #5fd6ff)"
            fill="none"
          />
        ))}
        {network.buses.map((bus, i) => (
          <g
            key={bus}
            data-bus={bus}
            transform={`translate(${layout.points[i].x - 90},${layout.points[i].y - 42})`}
          >
            <rect width="180" height="84" rx="8" />
            <text x="10" y="20">
              {bus}
              {i === network.slack ? ' · PCC' : ''}
            </text>
            <text x="10" y="39">
              设备 P{' '}
              {value(
                resources.reduce(
                  (a, r, j) => a + (r.bus === bus ? state.p_mw[j] : 0),
                  0,
                ),
                3,
              )}{' '}
              MW
            </text>
            <text x="10" y="56">
              设备 Q{' '}
              {value(
                resources.reduce(
                  (a, r, j) => a + (r.bus === bus ? state.q_mvar[j] : 0),
                  0,
                ),
                3,
              )}{' '}
              Mvar
            </text>
            <text x="10" y="73">
              V {interval(voltage?.[i], 3)}
            </text>
          </g>
        ))}
      </svg>
      <p>节点数值来自所选已完成周期，电压显示后台计算区间。</p>
    </div>
  );
}
