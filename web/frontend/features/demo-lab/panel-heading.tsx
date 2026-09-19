import { Activity } from 'lucide-react';
import type { ReactNode } from 'react';

export function PanelHeading({
  headingId,
  icon: Icon,
  title,
  trailing,
}: {
  headingId: string;
  icon: typeof Activity;
  title: string;
  trailing?: ReactNode;
}) {
  return (
    <div className="panel-title demo-panel-title">
      <h3 id={headingId}>
        <Icon aria-hidden="true" size={18} />
        {title}
      </h3>
      {trailing}
    </div>
  );
}
