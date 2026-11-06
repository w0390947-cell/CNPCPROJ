'use client';
import { ChevronRight, Network, Zap, type LucideIcon } from 'lucide-react';
export type PlatformPageId =
  | 'patent2'
  | 'demo_lab'
  | 'overview'
  | 'single_microgrid'
  | 'cluster_coordination'
  | 'communication_fault';
export type PlatformNavigationItem = {
  href: string;
  icon: LucideIcon;
  id: PlatformPageId;
  label: string;
};
const navigationItems: readonly PlatformNavigationItem[] = [
  {
    id: 'single_microgrid',
    label: '新能源最优协同控制算法仿真',
    href: '/single-microgrid',
    icon: Zap,
  },
  { id: 'patent2', label: '多层级微电网集群控制算法仿真', href: '/patent2', icon: Network },
];
type PlatformHeaderProps = {
  status: 'checking' | 'offline' | 'online';
  statusLabel?: string;
  timeLabel?: string;
};
const serviceStatusLabels = {
  checking: '正在连接仿真服务',
  offline: '仿真服务未连接',
  online: '仿真服务已连接',
};
export function PlatformHeader({
  status,
  statusLabel,
  timeLabel,
}: PlatformHeaderProps) {
  return (
    <header className="topbar">
      <div className="brand-mark">
        <Zap aria-hidden="true" size={21} />
        <i />
      </div>
      <div className="title-block">
        <h1>多层级微电网集群控制系统仿真平台</h1>
      </div>
      <div className="topbar-status">
        <span className={`system-ready ${status}`}>
          <i />
          {statusLabel ?? serviceStatusLabels[status]}
        </span>
        {timeLabel && <time>{timeLabel}</time>}
      </div>
    </header>
  );
}
type PlatformNavigationProps = {
  activeItem: PlatformPageId;
  isItemDisabled?: (item: PlatformNavigationItem) => boolean;
  onNavigate: (item: PlatformNavigationItem) => void;
};
export function PlatformNavigation({
  activeItem,
  isItemDisabled,
  onNavigate,
}: PlatformNavigationProps) {
  return (
    <aside className="sidebar">
      <nav aria-label="主导航">
        {navigationItems.map((item) => {
          const Icon = item.icon;
          const active = activeItem === item.id;
          return (
            <button
              aria-current={active ? 'page' : undefined}
              aria-label={item.label}
              className={active ? 'active' : undefined}
              disabled={isItemDisabled?.(item)}
              key={item.id}
              onClick={() => onNavigate(item)}
              title={item.label}
              type="button"
            >
              <Icon aria-hidden="true" size={20} />
              <span>{item.label}</span>
              {active && <ChevronRight aria-hidden="true" size={15} />}
            </button>
          );
        })}
      </nav>
    </aside>
  );
}
