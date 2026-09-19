import {
  Area,
  AreaChart,
  CartesianGrid,
  ReferenceDot,
  ReferenceLine,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from 'recharts';

import type { AvailabilityPoint } from '@/shared/api/generated/demo';

import { displayMeasurement } from './presentation';

function timeLabel(value: number) {
  const hours = Math.floor(value / 60)
    .toString()
    .padStart(2, '0');
  const minutes = (value % 60).toString().padStart(2, '0');
  return `${hours}:${minutes}`;
}

export function RenewableAvailabilityChart({
  actual,
  available,
  currentMinute,
  deviceId,
  draft,
  points,
}: {
  actual: number;
  available: number;
  currentMinute: number;
  deviceId: string;
  draft: number | null;
  points: AvailabilityPoint[];
}) {
  return (
    <figure
      aria-label={`${deviceId} 的合成资源基准曲线；当前可用 ${displayMeasurement(available)} MW，实际出力 ${displayMeasurement(actual)} MW。`}
      className="demo-resource-chart"
    >
      <ResponsiveContainer height="100%" width="100%">
        <AreaChart
          accessibilityLayer
          data={points}
          margin={{ top: 12, right: 18, bottom: 0, left: 0 }}
        >
          <defs>
            <linearGradient
              id="resourceAvailableFill"
              x1="0"
              x2="0"
              y1="0"
              y2="1"
            >
              <stop offset="0%" stopColor="#45d6a4" stopOpacity={0.35} />
              <stop offset="100%" stopColor="#45d6a4" stopOpacity={0.02} />
            </linearGradient>
          </defs>
          <CartesianGrid
            stroke="#203a4b"
            strokeDasharray="4 6"
            vertical={false}
          />
          <XAxis
            axisLine={false}
            dataKey="minute_of_day"
            domain={[0, 1425]}
            fontSize={11}
            stroke="#7897aa"
            tickFormatter={timeLabel}
            tickLine={false}
            ticks={[0, 360, 720, 1080, 1425]}
            type="number"
          />
          <YAxis
            axisLine={false}
            domain={[0, 'auto']}
            fontSize={11}
            stroke="#7897aa"
            tickLine={false}
            width={42}
          />
          <Tooltip
            contentStyle={{
              background: '#0b2231',
              border: '1px solid #245069',
              borderRadius: 8,
              color: '#dcecf4',
            }}
            labelFormatter={(value) => timeLabel(Number(value))}
          />
          <Area
            dataKey="p_available_mw"
            fill="url(#resourceAvailableFill)"
            name="基准可用功率 (MW)"
            stroke="#45d6a4"
            strokeWidth={2}
            type="stepAfter"
          />
          {draft !== null && (
            <ReferenceLine
              label={{ value: '输入 P', fill: '#f3c95d', fontSize: 11 }}
              stroke="#f3c95d"
              strokeDasharray="5 5"
              y={draft}
            />
          )}
          <ReferenceLine
            stroke="#91aabd"
            strokeDasharray="3 4"
            x={currentMinute}
          />
          <ReferenceDot
            fill="#39c9ff"
            r={4}
            stroke="#071721"
            strokeWidth={2}
            x={currentMinute}
            y={actual}
          />
          <ReferenceDot
            fill="#45d6a4"
            r={4}
            stroke="#071721"
            strokeWidth={2}
            x={currentMinute}
            y={available}
          />
        </AreaChart>
      </ResponsiveContainer>
    </figure>
  );
}
