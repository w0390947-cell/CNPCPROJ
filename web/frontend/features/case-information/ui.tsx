'use client';

import { ArrowUp, BookOpenText } from 'lucide-react';
import { useRouter } from 'next/navigation';

import {
  PlatformHeader,
  PlatformNavigation,
} from '@/shared/ui/platform-chrome';

import { CaseSections } from './case-sections';
import { CASE_SECTIONS } from './content';
import { StudySections } from './study-sections';
import styles from './style.module.css';

export function CaseInformation() {
  const router = useRouter();

  return (
    <main className={`control-room ${styles.controlRoom}`}>
      <a className="skip-link" href="#case-information-workspace">
        跳到算例与数据说明
      </a>
      <PlatformHeader status="online" statusLabel="说明资料已载入" />
      <PlatformNavigation
        activeItem="case_information"
        onNavigate={(item) => router.push(item.href)}
      />

      <section
        className={`workspace ${styles.workspace}`}
        id="case-information-workspace"
        tabIndex={-1}
      >
        <header className={styles.pageHeading}>
          <div>
            <div className={styles.eyebrow}>
              <BookOpenText size={16} aria-hidden="true" />
              算例参考
            </div>
            <h2>算例与数据说明</h2>
            <p>了解数据来源、研究设定与结果的适用范围。</p>
          </div>
          <span className={styles.dataTag}>参数化合成数据</span>
        </header>

        <div className={styles.readingLayout}>
          <nav aria-label="本页目录" className={styles.sectionNav}>
            <p className={styles.navHeading}>本页目录</p>
            <ol>
              {Object.values(CASE_SECTIONS).map((section) => (
                <li key={section.id}>
                  <a href={`#${section.id}`}>
                    <span aria-hidden="true">{section.number}</span>
                    {section.label}
                  </a>
                </li>
              ))}
            </ol>
            <a className={styles.backToTop} href="#case-information-workspace">
              <ArrowUp size={15} aria-hidden="true" />
              返回顶部
            </a>
          </nav>
          <div className={styles.chapters}>
            <CaseSections />
            <StudySections />
          </div>
        </div>
      </section>
    </main>
  );
}
