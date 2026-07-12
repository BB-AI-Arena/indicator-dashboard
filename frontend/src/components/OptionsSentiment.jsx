import { BarChart, Bar, XAxis, YAxis, Tooltip, ResponsiveContainer } from 'recharts'
import { formatCentralTime } from '../utils/time'

export default function OptionsSentiment({ ratios }) {
  const data = ratios?.ratios || []
  return (
    <div className="card p-4">
      <h3 className="mb-2 text-lg font-semibold">Options Sentiment</h3>
      <div className="mb-2 text-xs text-slate-400">
        Source: {ratios?.source || ratios?.provider || '-'} | Quote Type: {ratios?.quote_type || '-'} | Timestamp: {formatCentralTime(ratios?.timestamp)}
      </div>
      {ratios?.warning && <div className="mb-2 rounded bg-amber-900/30 p-2 text-xs text-amber-300">{ratios.warning}</div>}
      <div className="h-64">
        <ResponsiveContainer width="100%" height="100%">
          <BarChart data={data}>
            <XAxis dataKey="expiration" tick={{ fill: '#94a3b8', fontSize: 12 }} />
            <YAxis tick={{ fill: '#94a3b8', fontSize: 12 }} />
            <Tooltip />
            <Bar dataKey="call_volume" fill="#16c784" />
            <Bar dataKey="put_volume" fill="#ff4d5a" />
          </BarChart>
        </ResponsiveContainer>
      </div>
      <div className="mt-3 text-sm text-slate-300">Aggregate Put/Call Ratio: {ratios?.aggregate?.put_call_ratio ? ratios.aggregate.put_call_ratio.toFixed(2) : '-'}</div>
    </div>
  )
}
