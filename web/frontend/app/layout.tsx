import type { Metadata } from 'next';
import './globals.css';
import './dashboard.css';

export const metadata: Metadata = {
  title: '多层级微电网集群控制系统仿真平台',
  description: '油田源网荷储协同优化、集群协调与光伏群控策略验证平台',
};

export default function RootLayout({ children }: Readonly<{ children: React.ReactNode }>) {
  return <html lang="zh-CN" className="dark"><body>{children}</body></html>;
}
