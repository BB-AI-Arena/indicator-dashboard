import { useState } from 'react'

export default function TickerSearch({ onAnalyze, onAddWatchlist }) {
  const [value, setValue] = useState('META')

  const normalized = () => value.trim().toUpperCase()

  return (
    <div className="card p-4">
      <div className="flex flex-col gap-3 md:flex-row">
        <input
          className="w-full rounded border border-slate-700 bg-panel2 px-3 py-2 text-slate-100 focus:outline-none"
          placeholder="Enter ticker (e.g., META)"
          value={value}
          onChange={(e) => setValue(e.target.value)}
        />
        <button className="rounded bg-accent px-4 py-2 font-semibold text-slate-900" onClick={() => onAnalyze(normalized())}>Analyze</button>
        <button className="rounded bg-slate-700 px-4 py-2 font-semibold" onClick={() => onAddWatchlist(normalized())}>Add to Watchlist</button>
      </div>
    </div>
  )
}
