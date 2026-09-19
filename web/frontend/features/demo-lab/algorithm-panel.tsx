import { ExternalLink, FlaskConical } from 'lucide-react';

import { Button } from '@/components/ui/button';
import type { Catalog } from '@/shared/api/generated/demo';

import { PanelHeading } from './panel-heading';
import { algorithmDefinitions } from './presentation';

export type DemoJob = { name: string; url: string };

export function AlgorithmPanel({
  busy,
  job,
  presets,
  onRun,
}: {
  busy: boolean;
  job: DemoJob | null;
  presets: Catalog['presets'] | undefined;
  onRun: (key: string, request: Record<string, unknown>) => void;
}) {
  return (
    <section
      aria-labelledby="algorithms"
      className="panel demo-panel demo-algorithms"
    >
      <PanelHeading
        headingId="algorithms"
        icon={FlaskConical}
        title="运行算法"
      />
      <p className="demo-panel-copy">
        使用服务已捕获的合成资料包提交独立求解任务，运行进度和结果在对应算法页面查看。
      </p>
      <div className="demo-actions">
        {Object.entries(presets ?? {}).map(([key, request]) => (
          <Button disabled={busy} key={key} onClick={() => onRun(key, request)}>
            <FlaskConical aria-hidden="true" />
            {algorithmDefinitions[key]?.label ?? key}
          </Button>
        ))}
        {!presets && (
          <span className="demo-empty-inline">正在加载算法预设…</span>
        )}
      </div>
      {job && (
        <a className="demo-result-link" href={job.url}>
          查看 {job.name} 的任务进度与结果
          <ExternalLink aria-hidden="true" size={15} />
        </a>
      )}
    </section>
  );
}
