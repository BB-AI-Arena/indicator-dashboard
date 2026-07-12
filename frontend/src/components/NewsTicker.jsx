import { useEffect, useMemo, useState } from 'react'
import { api } from '../api'

function itemLabel(item) {
  const symbols = item.symbols?.length ? ` [${item.symbols.join(', ')}]` : ''
  return `${item.source || 'Market'}${symbols}: ${item.title}`
}

export default function NewsTicker() {
  const [feed, setFeed] = useState(null)
  const [error, setError] = useState('')

  useEffect(() => {
    let mounted = true
    const load = async () => {
      try {
        const data = await api.marketNews()
        if (!mounted) return
        setFeed(data)
        setError('')
      } catch (e) {
        if (!mounted) return
        setError(e.message || 'Market feed unavailable')
      }
    }
    load()
    const id = setInterval(load, 60000)
    return () => {
      mounted = false
      clearInterval(id)
    }
  }, [])

  const items = useMemo(() => (feed?.items || []).filter((item) => item?.title).slice(0, 24), [feed])
  const marqueeItems = items.length ? [...items, ...items] : []

  if (!items.length) {
    return (
      <div className="mb-4 overflow-hidden rounded border border-slate-800 bg-panel2 px-3 py-2 text-sm text-amber-200">
        RSS MARKET FEED {error ? `| ${error}` : '| Loading headlines'}
      </div>
    )
  }

  return (
    <div className="news-ticker mb-4 overflow-hidden rounded border border-slate-800 bg-panel2">
      <div className="flex items-center gap-0">
        <div className="z-10 shrink-0 border-r border-slate-700 bg-slate-950 px-3 py-2 text-xs font-black text-accent md:text-sm">
          RSS MARKET FEED
        </div>
        <div className="news-ticker-window">
          <div className="news-ticker-track">
            {marqueeItems.map((item, idx) => (
              <a
                key={`${item.link || item.title}-${idx}`}
                className="news-ticker-item"
                href={item.link || '#'}
                target="_blank"
                rel="noreferrer"
                title={itemLabel(item)}
              >
                <span className="font-semibold text-slate-400">{item.source}</span>
                {item.symbols?.length ? (
                  <span className="news-ticker-symbols">{item.symbols.slice(0, 4).join(' ')}</span>
                ) : null}
                <span>{item.title}</span>
              </a>
            ))}
          </div>
        </div>
      </div>
    </div>
  )
}
