'use client';

import type { SimulationResult } from '@/lib/simulation-api';
import type { SingleExampleInputs } from '../../shared/api/generated/single-example';
import type { LiveInitial } from '../../shared/api/simulation-live';
import { executionDisplayText } from '../../shared/lib/execution-display-text';
import { codedRegionText } from '../../shared/lib/coded-region-display';
import styles from '../patent2/style.module.css';
import pageStyles from './case-page.module.css';

const shown = (value: string | null | undefined, missing = '未记录') =>
  value ? codedRegionText(executionDisplayText(value)) : missing;

/** Read only the selected run. Preview metadata must never fill old archives. */
export function SingleProvenance({
  result,
  initial,
  preview,
  recordId,
  sourceLabel,
  runState = 'ready',
}: {
  result?: SimulationResult | null;
  initial?: LiveInitial;
  preview?: SingleExampleInputs | null;
  recordId?: string;
  sourceLabel?: string;
  runState?: 'ready' | 'running' | 'success' | 'error' | 'cancelled';
}) {
  const execution = result?.single_execution;
  const record = result ? execution?.input_record : initial?.input_record;
  const isPreview = !result && !initial && !recordId && runState === 'ready';
  const pending = !result && runState === 'running';
  const conditions = isPreview ? preview : null;
  const missing =
    isPreview && !conditions
      ? '条件尚未载入'
      : pending
        ? '待运行记录返回'
        : '未记录';
  const domain = result
    ? execution?.unified?.domain
    : (initial?.domain ?? conditions?.domain);
  const identity = result?.metadata ?? record ?? conditions;
  const solver = result?.request.solver;
  const policy = execution?.policy;
  return (
    <details className={styles.panel} aria-label="输入来源、版本与可追溯记录">
      <summary>输入来源、版本与可追溯记录</summary>
      <p>
        {isPreview
          ? '当前展示固定条件预览，运行信息将在计算后更新。'
          : pending
            ? '当前计算进行中，来源与版本信息随运行记录更新。'
            : '以下信息取自当前运行记录。'}
      </p>
      <dl className={pageStyles.provenance}>
        <dt>记录来源</dt>
        <dd>
          {shown(
            sourceLabel ??
              (isPreview ? '固定条件预览' : recordId ? '本次计算' : undefined),
            missing,
          )}
        </dd>
        <dt>输入来源</dt>
        <dd>
          {shown(
            execution?.input_basis ??
              record?.input_basis ??
              conditions?.input_basis,
            missing,
          )}
        </dd>
        <dt>边界来源</dt>
        <dd>{shown(domain?.bound_source, missing)}</dd>
        <dt>数据集标识 / 版本</dt>
        <dd>
          {identity?.dataset_id || identity?.dataset_revision
            ? `${shown(identity?.dataset_id, missing)} / ${shown(identity?.dataset_revision, missing)}`
            : missing}
        </dd>
        <dt>执行模型版本</dt>
        <dd>
          {shown(
            policy?.single_execution_version ??
              record?.execution_version ??
              conditions?.execution_version,
            missing,
          )}
        </dd>
        <dt>求解策略</dt>
        <dd>
          {solver
            ? `${result?.metadata.formulation === 'misocp' ? '混合整数二阶锥优化（MISOCP）' : shown(result?.metadata.formulation)}；${solver.quality_policy === 'quality-first-v1' ? '质量优先' : (solver.quality_policy ?? '手动预算')}；请求单次时限 ${solver.time_limit_seconds} s`
            : isPreview
              ? '待运行确认'
              : pending
                ? '待运行结果返回'
                : '未记录'}
          {policy?.computation_quality ? '；执行策略已记录质量控制参数' : ''}
        </dd>
        <dt>结果生成时间</dt>
        <dd>
          {shown(
            result?.metadata.generated_at,
            result
              ? '未记录'
              : isPreview
                ? '待运行生成'
                : pending
                  ? '待计算完成'
                  : '未生成结果',
          )}
        </dd>
      </dl>
    </details>
  );
}
