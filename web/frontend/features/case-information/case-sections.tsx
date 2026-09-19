import { Database, MapPinned, Network } from 'lucide-react';

import { CASE_SECTIONS, REGION_CASE_ROWS } from './content';
import { ReferenceSection } from './reference-section';
import styles from './style.module.css';

export function CaseSections() {
  return (
    <>
      <ReferenceSection section={CASE_SECTIONS.identity} icon={Database}>
        <p className={styles.intro}>
          负荷、风电、光伏、分时电价、网络参数和设备能力均用于软件在环研究。
          负荷与风光曲线由确定性函数生成，同一配置可重复得到同一组基础算例；
          通信故障和日内误差场景另使用固定随机种子。
        </p>
        <dl className={styles.identityList}>
          <div>
            <dt>山城区域</dt>
            <dd>SC 用于模拟山城区域，但不等同于经过现场核验的山城电网模型。</dd>
          </div>
          <div>
            <dt>跨区域演示</dt>
            <dd>YA_B、YA_C 是跨区域协调研究所需的合成演示区域。</dd>
          </div>
          <div>
            <dt>设备数据</dt>
            <dd>
              页面显示的“量测”和“设备实绩”是模拟设备状态，不是现场设备数据。
            </dd>
          </div>
        </dl>
      </ReferenceSection>

      <ReferenceSection section={CASE_SECTIONS.regions} icon={MapPinned}>
        <p className={styles.intro}>
          内部代码用于接口、设备 ID 和母线 ID；Web 使用面向用户的展示名称。
          下列容量为当前合成算例输入，并非现场设备台账。
        </p>
        {/* oxlint-disable jsx-a11y/no-noninteractive-tabindex -- Native scrolling needs keyboard focus; see README accessibility rationale. */}
        <section
          className={styles.tableWrap}
          aria-label="区域资产参数表，可横向滚动"
          tabIndex={0}
        >
          {/* oxlint-enable jsx-a11y/no-noninteractive-tabindex */}
          <table>
            <caption className="sr-only">
              内部区域代码、Web 展示名称及合成资产参数
            </caption>
            <thead>
              <tr>
                <th scope="col">区域 / 内部代码</th>
                <th scope="col">
                  负荷尺度<span>MW</span>
                </th>
                <th scope="col">
                  风电<span>MW</span>
                </th>
                <th scope="col">
                  光伏<span>MW</span>
                </th>
                <th scope="col">
                  储能<span>功率 / 容量</span>
                </th>
              </tr>
            </thead>
            <tbody>
              {REGION_CASE_ROWS.map((region) => (
                <tr key={region.id}>
                  <th scope="row">
                    <span className={styles.regionName}>
                      {region.displayName}
                    </span>
                    <code>{region.id}</code>
                  </th>
                  <td>{region.loadScaleMw}</td>
                  <td>{region.windMw}</td>
                  <td>{region.pvMw}</td>
                  <td className={styles.storageValue}>{region.storage}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </section>
      </ReferenceSection>

      <ReferenceSection section={CASE_SECTIONS.construction} icon={Network}>
        <dl className={styles.constructionList}>
          <div>
            <dt>网络结构</dt>
            <dd>
              每个区域采用 PCC、MAIN、WIND、PV、FLEX 五个母线和四条径向支路。
            </dd>
          </div>
          <div>
            <dt>负荷分配</dt>
            <dd>
              总有功负荷按 <strong>38% / 20% / 24% / 18%</strong> 分配到四个非
              PCC 母线。
            </dd>
          </div>
          <div>
            <dt>无功负荷</dt>
            <dd>
              负荷基础功率因数取 <strong>0.92 滞后</strong>
              ，据有功负荷生成对应无功负荷。
            </dd>
          </div>
          <div>
            <dt>风光曲线</dt>
            <dd>风光可用功率由平滑日曲线形成；它不是实时天气预测。</dd>
          </div>
          <div>
            <dt>经济性比较</dt>
            <dd>使用合成分时电价和参数化弃电、储能退化及网损成本。</dd>
          </div>
        </dl>
      </ReferenceSection>
    </>
  );
}
