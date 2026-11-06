import { RefreshCw } from 'lucide-react';

/** Record loading is owned by the page; selecting never submits a simulation. */
export function SavedRecordSelector({
  records,
  loading,
  disabled,
  refreshing = false,
  onSelect,
  onRefresh,
  className,
}: {
  records: readonly { id: string; label: string }[];
  loading: boolean;
  disabled: boolean;
  refreshing?: boolean;
  onSelect: (id: string) => void;
  onRefresh: () => void;
  className?: string;
}) {
  return (
    <div className={className}>
      <label>
        保存记录{' '}
        <select
          aria-label="选择已保存的真实运行记录"
          value=""
          disabled={disabled || loading}
          onChange={(event) => {
            if (event.target.value) onSelect(event.target.value);
          }}
        >
          <option value="">
            {loading
              ? '正在读取完整结果…'
              : records.length
                ? '选择一条示范计算'
                : '暂无已保存的示范计算'}
          </option>
          {records.map((record) => (
            <option key={record.id} value={record.id}>
              {record.label}
            </option>
          ))}
        </select>
      </label>
      <button
        aria-label="刷新保存记录"
        disabled={disabled || loading || refreshing}
        onClick={onRefresh}
      >
        <RefreshCw size={15} />
      </button>
    </div>
  );
}
