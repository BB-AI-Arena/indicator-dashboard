import { useEffect, useState } from 'react'
import { api } from '../api'
import { formatCentralTime } from '../utils/time'

export default function Alerts() {
  const [alerts, setAlerts] = useState([])
  const [error, setError] = useState('')

  useEffect(() => {
    api.alerts().then((d) => setAlerts(d.alerts || [])).catch((e) => setError(e.message))
  }, [])

  return (
    <div className="card p-4">
      <h2 className="mb-3 text-xl font-semibold">Recent Alerts</h2>
      {error && <div className="text-bear">{error}</div>}
      <div className="space-y-2">
        {alerts.map((a, idx) => (
          <div key={`${a.symbol}-${idx}`} className="rounded border border-slate-800 bg-panel2 p-3 text-sm">
            <span className="font-semibold">{a.symbol}</span> {a.side} | Score {a.score} | ${a.price?.toFixed?.(2)} | {formatCentralTime(a.timestamp)}
          </div>
        ))}
        {!alerts.length && !error && <div className="text-slate-400">No alerts yet.</div>}
      </div>
    </div>
  )
}
