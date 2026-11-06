import type { UnifiedExecution, UnifiedStep, TubeState } from '../../shared/api/generated/single-execution';
import { interval, value } from './presentation';
import styles from './style.module.css';
import { NetworkTopology } from './network-topology';

export function NetworkEvidence({ u, row, state, showTopology = true }: { u: UnifiedExecution; row?: UnifiedStep; state: TubeState; showTopology?: boolean }) {
  const net = u.domain.network;
  const cycle = row?.cycle_evidence;
  const path = cycle?.path_network;
  const endpoint = cycle?.endpoint_network;
  return <>
    <h3>径向网络与资源接入</h3>
    <p>{net.buses.length} 个节点、{net.branches.length} 条支路；基准 {net.base_mva} MVA。节点显示已完成的设备出力汇总；电压显示后台证书区间。</p>
    {showTopology && <NetworkTopology network={net} resources={u.domain.devices.resources} state={state} voltage={endpoint?.voltage_bounds} label="实际网络接线与节点证据" />}
    {!cycle ? <p className={styles.notice}>当前周期未记录过程网络与状态盒明细。历史结果保留原结论，页面不补造证书；新运行将保存这些证据。</p> : <>
      <div className={styles.stats}>
        <div><span>后台过程网络认证</span><strong>{path?.certified ? '通过' : '未获证'}</strong></div>
        <div><span>后台端点网络认证</span><strong>{endpoint?.certified ? '通过' : '未获证'}</strong></div>
        <div><span>过程压缩系数上界</span><strong>{value(path?.contraction_upper)}</strong></div>
        <div><span>高电压解唯一性系数上界</span><strong>{value(path?.high_voltage_uniqueness_upper)}</strong></div>
      </div>
      <p>过程 PCC P {interval(path?.pcc_p_mw)} MW；Q {interval(path?.pcc_q_mvar)} Mvar。受电限值 [{net.pcc_min_mw}, {net.pcc_max_mw}] MW；功率因数下限 {net.pf_min}。这些区间和认证结论均来自后台，页面不重新进行潮流或安全认证。</p>
      <div className={styles.scroll}><table><caption>节点电压包络 · pu；运行范围 [{net.voltage_min_pu}, {net.voltage_max_pu}]</caption><thead><tr><th>节点</th><th>全过程外包</th><th>周期末外包</th></tr></thead><tbody>
        {net.buses.map((bus, i) => <tr key={bus}><th scope="row">{bus}</th><td>{interval(path?.voltage_bounds?.[i])}</td><td>{interval(endpoint?.voltage_bounds?.[i])}</td></tr>)}
      </tbody></table></div>
      <div className={styles.scroll}><table><caption>支路容量与电流证据</caption><thead><tr><th>支路</th><th>容量 / MVA</th><th>全过程容量上界 / MVA</th><th>端点容量上界 / MVA</th><th>过程电流 / pu</th><th>r / x / 变比</th></tr></thead><tbody>
        {net.branches.map((b, i) => <tr key={i}><th scope="row">{net.buses[b.parent]} → {net.buses[b.child]}</th><td>{value(b.rating_mva, 3)}</td><td>{value(path?.branch_mva_upper?.[i])}</td><td>{value(endpoint?.branch_mva_upper?.[i])}</td><td>{interval(path?.branch_current_pu_bounds?.[i])}</td><td>{b.r_pu} / {b.x_pu} / {b.tap}</td></tr>)}
      </tbody></table></div>
      <div className={styles.scroll}><table><caption>设备状态盒 · P 为 MW，Q 为 Mvar</caption><thead><tr><th>资源</th><th>源 P / Q</th><th>全过程 P / Q</th><th>响应端点 P / Q</th><th>保留证书下一盒 P / Q</th></tr></thead><tbody>
        {u.domain.devices.resources.map((r, i) => <tr key={r.resource_id}><th scope="row">{r.resource_id}</th><td>{interval(cycle.transition.initial.p_mw[i])} / {interval(cycle.transition.initial.q_mvar[i])}</td><td>{interval(cycle.transition.path.p_mw[i])} / {interval(cycle.transition.path.q_mvar[i])}</td><td>{interval(cycle.transition.endpoint.p_mw[i])} / {interval(cycle.transition.endpoint.q_mvar[i])}</td><td>{interval(cycle.expected_next.p_mw[i])} / {interval(cycle.expected_next.q_mvar[i])}</td></tr>)}
      </tbody></table></div>
      <p>过程电量 {interval(cycle.transition.path.energy_mwh)} MWh；端点电量 {interval(cycle.transition.endpoint.energy_mwh)} MWh。证书所声明的平衡范围：{path?.balance_scope}。</p>
    </>}
    <details><summary>查看输入负荷及风光保证可用量</summary><div className={styles.scroll}><table><caption>本周期观测与声明区间</caption><thead><tr><th>母线</th><th>观测 P / MW</th><th>声明 P / MW</th><th>观测 Q / Mvar</th><th>声明 Q / Mvar</th></tr></thead><tbody>
      {u.domain.forecasts[Math.min(row?.step ?? 0, u.domain.forecasts.length - 1)]?.loads.map((b) => {
        const observed = row?.observed.loads.find((l) => l.bus === b.bus);
        return <tr key={b.bus}><th scope="row">{b.bus}</th><td>{interval(observed?.p_mw)}</td><td>{interval(b.p_mw)}</td><td>{interval(observed?.q_mvar)}</td><td>{interval(b.q_mvar)}</td></tr>;
      })}
    </tbody></table></div></details>
  </>;
}
