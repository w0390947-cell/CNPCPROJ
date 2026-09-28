import styles from './style.module.css';

export type ClusterView = 'plan' | 'process' | 'execution';

const views = [
  {
    value: 'plan',
    label: '运行态势',
    description:
      '按原始计划时刻查看全天功率安排，可选择区域创建新能源事件分支。',
  },
  {
    value: 'process',
    label: '协调过程',
    description:
      '按 ADMM 迭代轮次查看申报、参考和收敛情况；算法轮次不等于滚动窗口数。',
  },
  {
    value: 'execution',
    label: '执行校核',
    description: '查看日前、日内与分钟级模拟执行证据，定位未通过项及对应时段。',
  },
] as const;

export function ClusterWorkspaceViews({
  value,
  onChange,
  busy,
}: {
  value: ClusterView;
  onChange: (value: ClusterView) => void;
  busy: boolean;
}) {
  return (
    <div className={styles.workspaceViews}>
      <div className={styles.toolbar}>
        <fieldset className={styles.modes} aria-label="集群结果视图">
          {views.map((view) => (
            <button
              key={view.value}
              type="button"
              aria-pressed={value === view.value}
              disabled={busy}
              onClick={() => onChange(view.value)}
            >
              {view.label}
            </button>
          ))}
        </fieldset>
        <span className={styles.note}>
          共享同一次仿真结果 · 切换视图不重新计算
        </span>
      </div>
      <p className={styles.note}>
        {views.find((view) => view.value === value)?.description}
      </p>
    </div>
  );
}
