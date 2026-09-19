import { BookOpenText, Clock3, Gauge, ShieldCheck } from 'lucide-react';

import { CASE_SECTIONS, GLOSSARY, STUDY_LIMITS } from './content';
import { ReferenceSection } from './reference-section';
import styles from './style.module.css';

export function StudySections() {
  return (
    <>
      <ReferenceSection section={CASE_SECTIONS.limits} icon={Gauge}>
        <p className={styles.intro}>
          下列数值是软件当前使用的研究参数，不是程序从现场自动推导的正式运行定值；
          实际部署前应由甲方和调度专业确认。
        </p>
        <dl className={styles.limitGrid}>
          {STUDY_LIMITS.map((limit) => (
            <div key={limit.name}>
              <dt>{limit.name}</dt>
              <dd>
                <strong>{limit.value}</strong>
                <p>{limit.meaning}</p>
              </dd>
            </div>
          ))}
        </dl>
      </ReferenceSection>

      <ReferenceSection section={CASE_SECTIONS.time} icon={Clock3}>
        <dl className={styles.timeGrid}>
          <div>
            <dt>日前优化</dt>
            <dd>
              <strong>
                24 小时 <span>/ 96 点</span>
              </strong>
              <p>完整配置每点 15 分钟；快速预设是覆盖同一整天的降采样演示。</p>
            </dd>
          </div>
          <div>
            <dt>ADMM 协调</dt>
            <dd>
              <strong>算法迭代轮次</strong>
              <p>时间轴表示迭代轮次，不应换算成分钟或现场时刻。</p>
            </dd>
          </div>
          <div>
            <dt>模拟设备</dt>
            <dd>
              <strong>
                1 分钟 <span>/ 周期</span>
              </strong>
              <p>
                持续服务每个周期推进一分钟模拟时间，用于回放设备响应和保护逻辑。
              </p>
            </dd>
          </div>
        </dl>
      </ReferenceSection>

      <ReferenceSection section={CASE_SECTIONS.boundary} icon={ShieldCheck}>
        <ol className={styles.layerFlow}>
          <li>
            <b>集中式基准</b>
            <span>同一合成输入下的性能参照</span>
          </li>
          <li>
            <b>ADMM 协调参考</b>
            <span>形成各区域 PCC 的 P/Q 参考轨迹</span>
          </li>
          <li>
            <b>区域计划落实</b>
            <span>区域 MISOCP 将参考分解到风、光、储、SVG</span>
          </li>
          <li>
            <b>设备执行</b>
            <span>一分钟跟踪、命令回执与本地保护</span>
          </li>
        </ol>
        <aside className={styles.boundaryNote} aria-label="校核范围说明">
          <strong>校核通过的范围，以当前结果声明为准</strong>
          <p>
            公共“集群协调”和“通信故障”页面当前只校核集中式基准与 ADMM 协调参考；
            若页面提示“分布式执行未校核”，即表示后两层不能据此认定通过。
            “全部通过”始终只针对当前结果明确声明的校核范围。
          </p>
        </aside>
      </ReferenceSection>

      <ReferenceSection section={CASE_SECTIONS.glossary} icon={BookOpenText}>
        <dl className={styles.glossary}>
          {GLOSSARY.map(([term, description]) => (
            <div key={term}>
              <dt>{term}</dt>
              <dd>{description}</dd>
            </div>
          ))}
        </dl>
        <div className={styles.signNote}>
          <strong>功率正方向：两种边界的符号约定不能混用</strong>
          <dl>
            <div>
              <dt>优化结果 · PCC 有功</dt>
              <dd>正值表示从上级电网受电。</dd>
            </div>
            <div>
              <dt>模拟设备命令 · P/Q</dt>
              <dd>正值表示向网络注入；储能充电对应负 P。</dd>
            </div>
          </dl>
        </div>
      </ReferenceSection>
    </>
  );
}
