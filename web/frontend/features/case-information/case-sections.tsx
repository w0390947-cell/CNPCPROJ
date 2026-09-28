import { Database, MapPinned, Network } from 'lucide-react';

import { CASE_SECTIONS, REGION_CASE_ROWS, DATASET_IDENTITY } from './content';
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
            <dt>统一数据版本</dt>
            <dd>
              {DATASET_IDENTITY.dataset_id} · {DATASET_IDENTITY.revision}
            </dd>
          </div>
          <div>
            <dt>山城区域</dt>
            <dd>SC 用于模拟山城区域，但不等同于经过现场核验的山城电网模型。</dd>
          </div>
          <div>
            <dt>跨区域演示</dt>
            <dd>
              当前版本 YA_B 参考化子坪、YA_C 参考榆树资料；两地各采用一台 5 MW
              风机，仍包含模拟设备和运行数据。历史结果保留原算例参数。
            </dd>
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
                  负荷峰值参数合计<span>MW</span>
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
              {REGION_CASE_ROWS.map(
                (region) =>
                  `${region.displayName}：${region.busCount} 个母线、${region.branchCount} 条支路`,
              ).join('；')}
              。 山城参考资料中的 35 kV 并网结构，风电、储能与 SVG
              分支接入；两处光伏及其 35/10 kV
              变压器为保留的模拟扩展。延安两区参考 35/10 kV
              站级结构：化子坪风机按化镰线 T 接，榆树风机接入 35 kV
              系统；双主变采用并列等值。光伏、储能、SVG及负荷曲线仍为模拟配置，
              电容器容量参考资料，投退状态尚未确认。
            </dd>
          </div>
          <div>
            <dt>负荷分配</dt>
            <dd>
              各母线负荷按统一台账中的峰值参数及固定种子生成；PCC
              母线不叠加本地负荷。
            </dd>
          </div>
          <div>
            <dt>无功负荷</dt>
            <dd>按各母线台账中的滞后功率因数，由有功负荷生成对应无功负荷。</dd>
          </div>
          <div>
            <dt>风光曲线</dt>
            <dd>
              同一台账生成日前预测、日内预测与分钟实际曲线。三者保留不同误差种子与用途；设备演示使用分钟实际曲线，优化页面使用日前预测。
            </dd>
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
