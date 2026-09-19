import type { LucideIcon } from 'lucide-react';
import type { ReactNode } from 'react';

import { CASE_SECTIONS } from './content';
import styles from './style.module.css';

type ReferenceSectionProps = {
  section: (typeof CASE_SECTIONS)[keyof typeof CASE_SECTIONS];
  icon: LucideIcon;
  children: ReactNode;
};

/** Feature-local chapter framing; domain content stays with its section. */
export function ReferenceSection({
  section,
  icon: Icon,
  children,
}: ReferenceSectionProps) {
  return (
    <section
      className={styles.section}
      id={section.id}
      aria-labelledby={`${section.id}-title`}
    >
      <header className={styles.sectionHeading}>
        <span className={styles.sectionNumber} aria-hidden="true">
          {section.number}
        </span>
        <h3 id={`${section.id}-title`}>{section.title}</h3>
        <Icon size={20} aria-hidden="true" />
      </header>
      {children}
    </section>
  );
}
