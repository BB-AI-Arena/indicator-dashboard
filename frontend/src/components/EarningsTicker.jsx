import { useEffect, useMemo, useState } from 'react'
import { api } from '../api'

function label(item) {
  const eps = item.eps_forecast ? ` EPS est. ${item.eps_forecast}` : ''
  return `${item.symbol} ${item.time} ${item.date}${eps}`
}

export default function EarningsTicker() {
  const [feed, setFeed] = useState(null)
  const [error, setError] = useState('')

  useEffect(() => {
    let mounted = true
    const load = async () => {
      try {
        const data = await api.earningsNews()
        if (!mounted) return
        setFeed(data)
        setError('')
      } catch (e) {
        if (!mounted) return
        setError(e.message || 'Earnings feed unavailable')
      }
    }
    load()
    const id = setInterval(load, 300000)
    return () => {
      mounted = false
      clearInterval(id)
    }
  }, [])

  const items = useMemo(() => (feed?.items || []).filter((item) => item?.symbol).slice(0, 32), [feed])
  const marqueeItems = items.length ? [...items, ...items] : []

  if (!items.length) {
    return (
      <div className="mb-4 overflow-hidden rounded border border-slate-800 bg-panel2 px-3 py-2 text-sm text-slate-300">
        EARNINGS THIS WEEK {error ? `| ${error}` : '| No remaining watchlist reports this week'}
      </div>
    )
  }

  return (
    <div className="news-ticker earnings-ticker mb-4 overflow-hidden rounded border border-slate-800 bg-panel2">
      <div className="flex items-center gap-0">
        <div className="z-10 shrink-0 border-r border-slate-700 bg-slate-950 px-3 py-2 text-xs font-black text-amber-300 md:text-sm">
          EARNINGS THIS WEEK
        </div>
        <div className="news-ticker-window">
          <div className="news-ticker-track earnings-ticker-track">
            {marqueeItems.map((item, idx) => (
              <div
                key={`${item.symbol}-${item.date}-${item.time}-${idx}`}
                className="news-ticker-item earnings-ticker-item"
                title={label(item)}
              >
                <span className="news-ticker-symbols">{item.symbol}</span>
                <span className="font-semibold text-slate-300">{item.time}</span>
                <span>{item.date}</span>
                <span className="max-w-[18rem] truncate text-slate-200">{item.name}</span>
                {item.eps_forecast ? <span className="text-amber-200">EPS {item.eps_forecast}</span> : null}
              </div>
            ))}
          </div>
        </div>
      </div>
    </div>
  )
}
