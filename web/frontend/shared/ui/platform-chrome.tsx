'use client';

import {
  Activity,
  BookOpenText,
  ChevronRight,
  CircleGauge,
  Gauge,
  Network,
  Zap,
  type LucideIcon,
} from 'lucide-react';

export type PlatformPageId =
  | 'demo_lab'
  | 'overview'
  | 'single_microgrid'
  | 'cluster_coordination'
  | 'communication_fault'
  | 'group_control'
  | 'case_information';

export type PlatformNavigationItem = {
  href: string;
  icon: LucideIcon;
  id: PlatformPageId;
  label: string;
};

const navigationItems: readonly PlatformNavigationItem[] = [
  { id: 'demo_lab', label: '模拟设备演示', href: '/demo-lab', icon: Zap },
  { id: 'overview', label: '综合态势', href: '/', icon: Gauge },
  {
    id: 'single_microgrid',
    label: '单微网优化',
    href: '/single-microgrid',
    icon: Zap,
  },
  {
    id: 'cluster_coordination',
    label: '集群协调',
    href: '/cluster-coordination',
    icon: Network,
  },
  {
    id: 'communication_fault',
    label: '通信故障',
    href: '/communication-fault',
    icon: Activity,
  },
  {
    id: 'group_control',
    label: '光伏群控策略验证',
    href: '/group-control',
    icon: CircleGauge,
  },
  {
    id: 'case_information',
    label: '算例与数据说明',
    href: '/case-information',
    icon: BookOpenText,
  },
];

type PlatformHeaderProps = {
  status: 'checking' | 'offline' | 'online';
  statusLabel: string;
  timeLabel?: string;
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
          {statusLabel}
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
