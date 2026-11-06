import type {
  Patent2Inputs,
  Patent2Region,
} from '@/shared/api/generated/patent2';
import { labels, number } from './model';
import styles from './style.module.css';

function layout(region: Patent2Region) {
  const children = new Map<string, string[]>();
  region.branches.forEach((b) =>
    children.set(b.parent, [...(children.get(b.parent) ?? []), b.child]),
  );
  const root =
    region.buses.find((b) => !region.branches.some((e) => e.child === b)) ??
    region.buses[0];
  const points = new Map<string, { x: number; y: number }>();
  let leaf = 0;
  function place(bus: string, depth: number) {
    if (points.has(bus)) return;
    points.set(bus, { x: depth, y: 0 });
    const next = children.get(bus) ?? [];
    next.forEach((c) => place(c, depth + 1));
    points.set(bus, {
      x: depth,
      y: next.length
        ? next.reduce((sum, c) => sum + points.get(c)!.y, 0) / next.length
        : leaf++,
    });
  }
  place(root, 0);
  const depth = Math.max(1, ...[...points.values()].map((p) => p.x));
  const xy = (bus: string) => {
    const p = points.get(bus)!;
    return {
      x: 60 + (p.x / depth) * 540,
      y: 45 + (p.y / Math.max(1, leaf - 1)) * 180,
    };
  };
  return { root, xy };
}
export function Topology({ region }: { region: Patent2Region }) {
  const { root, xy } = layout(region);
  return (
    <svg
      className={styles.topology}
      viewBox="0 0 680 280"
      aria-label={`${labels[region.name]}完整径向网络`}
    >
      <title>{labels[region.name]}完整径向网络</title>
      {region.branches.map((b, i) => {
        const a = xy(b.parent),
          z = xy(b.child);
        return (
          <g key={b.name}>
            <title>{`B${i + 1} · ${b.name}: ${b.parent} → ${b.child}`}</title>
            <line
              x1={a.x}
              y1={a.y}
              x2={z.x}
              y2={z.y}
              stroke="var(--plot-slate, #5c899a)"
              strokeWidth="2"
            />
            <text
              x={(a.x + z.x) / 2}
              y={(a.y + z.y) / 2 - 9}
              textAnchor="middle"
            >
              B{i + 1}
            </text>
          </g>
        );
      })}
      {region.buses.map((b, i) => {
        const p = xy(b);
        return (
          <g key={b}>
            <circle
              cx={p.x}
              cy={p.y}
              r="11"
              fill={
                b === root
                  ? 'var(--plot-orange, #f7bc73)'
                  : 'var(--plot-green, #53d8c1)'
              }
            />
            <text x={p.x} y={p.y + 30} textAnchor="middle">
              N{i} · {b}
            </text>
            {b === root && (
              <text x={p.x} y={p.y - 22} textAnchor="middle">
                PCC
              </text>
            )}
          </g>
        );
      })}
    </svg>
  );
}

export function Inputs({ inputs }: { inputs: Patent2Inputs }) {
  const sum = (r: Record<string, number[]>, i: number) =>
    Object.values(r).reduce((s, v) => s + (v[i] ?? 0), 0);
  return (
    <div className={styles.stack}>
      <section className={styles.panel}>
        <h2>共同条件</h2>
        <p>
          从原始日曲线取 00:00—01:00 的连续四个 15 分钟时段；执行步长 1
          分钟，滚动更新间隔 15
          分钟。正常与低电量工况使用相同网络、负荷、风光和设备参数，只改变储能初始电量。
        </p>
        <div className={styles.facts}>
          <span>
            三区域联合受电上限 <b>{inputs.cluster_limit_mw} MW</b>
          </span>
          <span>
            区域规划功率因数 <b>0.92</b>
          </span>
          <span>
            集群功率因数 <b>0.95</b>
          </span>
          <span>
            连续区域功率因数 <b>0.90</b>
          </span>
          <span>
            滚动展望上限 <b>240 min，受研究末端截断</b>
          </span>
          <span>
            基础备用 <b>0.05 + 0.02 × 区域规模（MW）</b>
          </span>
        </div>
        <p className={styles.muted}>
          本算例采用基础功率备用预留，并分别校核连续执行安全与完整备用调用能力；未启用额外短窗口重规划或备用预留增强功能。各区域的母线编号
          N 和支路编号 B 独立编排，与本页面对应区域下方“展开母线、支路和设备初值”中的表格一致。
        </p>
      </section>
      {inputs.regions.map((r) => (
        <section className={styles.panel} key={r.name}>
          <h2>
            {labels[r.name]}{' '}
            <small>
              {r.name} · {r.buses.length} 母线 / {r.branches.length} 支路
            </small>
          </h2>
          <div className={styles.stack}>
            <div className={styles.networkScroll}>
              <Topology region={r} />
            </div>
            <div>
              <h3>初始工况与储能</h3>
              <dl className={styles.facts}>
                <div>
                  初始电量 <b>{number(r.initial_energy_mwh)} MWh</b>
                </div>
                <div>
                  物理电量边界{' '}
                  <b>
                    {number(r.minimum_energy_mwh)}—
                    {number(r.maximum_energy_mwh)} MWh
                  </b>
                </div>
                <div>
                  额定充放电功率 <b>{number(r.storage_power_mw)} MW</b>
                </div>
                <div>
                  充 / 放电效率{' '}
                  <b>
                    {number(r.eta_charge, 2)} / {number(r.eta_discharge, 2)}
                  </b>
                </div>
              </dl>
              <p>
                储能功率正值为放电、负值为充电。初始设备出力来自输入快照，未假定所有设备均从零启动。
              </p>
            </div>
          </div>
          <div className={styles.tableWrap}>
            <table>
              <caption>连续时段输入（区域合计）</caption>
              <thead>
                <tr>
                  <th>原始时段</th>
                  <th>负荷 P / MW</th>
                  <th>负荷 Q / Mvar</th>
                  <th>风电可用 / MW</th>
                  <th>光伏可用 / MW</th>
                  <th>电价 / 元·MWh⁻¹</th>
                </tr>
              </thead>
              <tbody>
                {[0, 1, 2, 3].map((i) => (
                  <tr key={i}>
                    <td>
                      {i * 15}—{(i + 1) * 15} min
                    </td>
                    <td>{number(sum(r.load_p_mw, i))}</td>
                    <td>{number(sum(r.load_q_mvar, i))}</td>
                    <td>{number(sum(r.wind_available_mw, i))}</td>
                    <td>{number(sum(r.pv_available_mw, i))}</td>
                    <td>{inputs.price_cny_per_mwh[i]}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          <details>
            <summary>展开母线、支路和设备初值</summary>
            <div className={styles.tableWrap}>
              <table>
                <thead>
                  <tr>
                    <th>母线</th>
                    <th>负荷 P（四时段 MW）</th>
                    <th>负荷 Q（Mvar）</th>
                    <th>风电（MW）</th>
                    <th>光伏（MW）</th>
                  </tr>
                </thead>
                <tbody>
                  {r.buses.map((b, i) => (
                    <tr key={b}>
                      <td>
                        N{i} · {b}
                      </td>
                      {[
                        r.load_p_mw,
                        r.load_q_mvar,
                        r.wind_available_mw,
                        r.pv_available_mw,
                      ].map((d, k) => (
                        <td key={k}>
                          {d[b]?.map((v) => number(v)).join(' / ') ?? '无'}
                        </td>
                      ))}
                    </tr>
                  ))}
                </tbody>
              </table>
              <table>
                <thead>
                  <tr>
                    <th>支路</th>
                    <th>起点 → 终点</th>
                    <th>R / pu</th>
                    <th>X / pu</th>
                    <th>容量 / MVA</th>
                    <th>变比</th>
                  </tr>
                </thead>
                <tbody>
                  {r.branches.map((b, i) => (
                    <tr key={b.name}>
                      <td>
                        B{i + 1} · {b.name}
                      </td>
                      <td>
                        {b.parent} → {b.child}
                      </td>
                      <td>{number(b.r_pu, 6)}</td>
                      <td>{number(b.x_pu, 6)}</td>
                      <td>{number(b.s_max_mva)}</td>
                      <td>{number(b.tap_ratio)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
              <table>
                <thead>
                  <tr>
                    <th>设备</th>
                    <th>初始 P / MW</th>
                    <th>初始 Q / Mvar</th>
                  </tr>
                </thead>
                <tbody>
                  {[
                    ...new Set([
                      ...Object.keys(r.initial_p_mw),
                      ...Object.keys(r.initial_q_mvar),
                    ]),
                  ].map((id) => (
                    <tr key={id}>
                      <td>{id}</td>
                      <td>{number(r.initial_p_mw[id])}</td>
                      <td>{number(r.initial_q_mvar[id])}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </details>
        </section>
      ))}
    </div>
  );
}
