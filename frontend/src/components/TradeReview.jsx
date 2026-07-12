import { useEffect, useMemo, useRef, useState } from 'react'
import { api } from '../api'
import TickerChart from './TickerChart'
import { formatCentralTime } from '../utils/time'
import MoneyFlowPanel from './MoneyFlowPanel'
import NewsCatalystPanel from './NewsCatalystPanel'

function money(value, digits = 2) {
  const n = Number(value)
  return Number.isFinite(n) ? `$${n.toFixed(digits)}` : '-'
}

function pct(value, digits = 2) {
  const n = Number(value)
  return Number.isFinite(n) ? `${n.toFixed(digits)}%` : '-'
}

function secondsToText(value) {
  const n = Number(value)
  if (!Number.isFinite(n) || n <= 0) return '-'
  const totalMinutes = Math.round(n / 60)
  const days = Math.floor(totalMinutes / (60 * 24))
  const hours = Math.floor((totalMinutes - days * 24 * 60) / 60)
  const minutes = totalMinutes - days * 24 * 60 - hours * 60
  const parts = []
  if (days) parts.push(`${days}d`)
  if (hours) parts.push(`${hours}h`)
  if (minutes || !parts.length) parts.push(`${minutes}m`)
  return parts.join(' ')
}

function textOrDash(value) {
  if (Array.isArray(value)) {
    const rows = value.filter(Boolean)
    return rows.length ? rows.join(' | ') : '-'
  }
  if (value === null || value === undefined || value === '') return '-'
  return String(value)
}

function toneForGrade(value) {
  const grade = String(value || '').toUpperCase()
  if (grade === 'A' || grade === 'B') return 'border-emerald-700/60 bg-emerald-900/30 text-emerald-200'
  if (grade === 'C') return 'border-amber-700/60 bg-amber-900/30 text-amber-200'
  if (grade === 'D' || grade === 'F') return 'border-red-700/60 bg-red-900/30 text-red-200'
  return 'border-slate-700 bg-panel2 text-slate-200'
}

function toneForPnl(value) {
  const n = Number(value)
  if (!Number.isFinite(n)) return 'text-slate-200'
  if (n > 0) return 'text-emerald-300'
  if (n < 0) return 'text-red-300'
  return 'text-slate-200'
}

function chipClass(active) {
  return active
    ? 'border-emerald-700/60 bg-emerald-900/30 text-emerald-200'
    : 'border-slate-700 bg-panel2 text-slate-300'
}

function List({ items }) {
  const rows = Array.isArray(items) ? items.filter(Boolean) : []
  if (!rows.length) return <p className="text-slate-400">-</p>
  return (
    <ul className="space-y-1">
      {rows.map((item, idx) => <li key={`${idx}-${item}`}>{item}</li>)}
    </ul>
  )
}

function SummaryStat({ label, value, tone = 'text-slate-100' }) {
  return (
    <div className="card p-3">
      <p className="text-[11px] uppercase text-slate-400">{label}</p>
      <p className={`mt-1 text-lg font-semibold ${tone}`}>{value}</p>
    </div>
  )
}

export default function TradeReview({ currentUser }) {
  const [overview, setOverview] = useState(null)
  const [detail, setDetail] = useState(null)
  const [error, setError] = useState('')
  const [detailError, setDetailError] = useState('')
  const [loading, setLoading] = useState(true)
  const [detailLoading, setDetailLoading] = useState(false)
  const [busy, setBusy] = useState(false)
  const [syncBusy, setSyncBusy] = useState(false)
  const [selectedTradeId, setSelectedTradeId] = useState(null)
  const [accountMode, setAccountMode] = useState('EXPLICIT')
  const [selectedAccountRefs, setSelectedAccountRefs] = useState([])
  const [draftFilters, setDraftFilters] = useState({
    from_date: '',
    to_date: '',
    ticker: '',
    call_put: '',
    winner_loser: '',
    grade: '',
    dte_bucket: '',
    setup_type: '',
    market_regime: '',
    reviewed: '',
    limit: 200,
  })
  const [appliedFilters, setAppliedFilters] = useState({
    from_date: '',
    to_date: '',
    ticker: '',
    call_put: '',
    winner_loser: '',
    grade: '',
    dte_bucket: '',
    setup_type: '',
    market_regime: '',
    reviewed: '',
    limit: 200,
  })
  const [adminNotes, setAdminNotes] = useState('')
  const [reviewed, setReviewed] = useState(false)
  const loadedOnce = useRef(false)

  const loadOverview = async (filters = appliedFilters, keepSelection = true) => {
    if (!loadedOnce.current) setLoading(true)
    setBusy(true)
    setError('')
    try {
      const res = await api.tradeReviewOverview(filters)
      setOverview(res)
      const selection = res?.selection || {}
      if (!keepSelection || !loadedOnce.current) {
        setAccountMode(selection.selection_mode || 'EXPLICIT')
        setSelectedAccountRefs(Array.isArray(selection.selected_account_refs) ? selection.selected_account_refs : [])
      }
      const trades = Array.isArray(res?.trades) ? res.trades : []
      if (!selectedTradeId && trades.length) {
        setSelectedTradeId(trades[0].id)
      } else if (selectedTradeId && !trades.some((trade) => trade.id === selectedTradeId) && trades.length) {
        setSelectedTradeId(trades[0].id)
      } else if (!trades.length) {
        setSelectedTradeId(null)
        setDetail(null)
      }
    } catch (e) {
      setError(e.message)
    } finally {
      loadedOnce.current = true
      setLoading(false)
      setBusy(false)
    }
  }

  const loadDetail = async (tradeId) => {
    if (!tradeId) {
      setDetail(null)
      setDetailError('')
      return
    }
    setDetailLoading(true)
    setDetailError('')
    try {
      const res = await api.tradeReviewTradeDetail(tradeId, false, true)
      setDetail(res)
      setAdminNotes(res?.trade?.admin_notes || '')
      setReviewed(Boolean(res?.trade?.reviewed))
    } catch (e) {
      setDetailError(e.message)
    } finally {
      setDetailLoading(false)
    }
  }

  useEffect(() => {
    if (currentUser?.role !== 'admin') return
    loadOverview(appliedFilters, false)
  }, [currentUser?.username])

  useEffect(() => {
    if (!selectedTradeId) return
    loadDetail(selectedTradeId)
  }, [selectedTradeId])

  useEffect(() => {
    const id = setInterval(() => {
      if (overview?.sync?.status === 'RUNNING' || overview?.sync?.status === 'PENDING') {
        loadOverview(appliedFilters, true)
      }
    }, 12000)
    return () => clearInterval(id)
  }, [overview?.sync?.status, JSON.stringify(appliedFilters)])

  const accountMap = useMemo(() => {
    const map = new Map()
    ;(overview?.accounts || []).forEach((account) => {
      map.set(account.account_ref, account)
    })
    return map
  }, [overview])

  const selectedTrade = useMemo(() => {
    return (overview?.trades || []).find((trade) => trade.id === selectedTradeId) || null
  }, [overview, selectedTradeId])

  const selectedTradeDetail = detail?.trade || null
  const chartData = selectedTradeDetail?.chart || null
  const tradeMarkers = Array.isArray(chartData?.markers) ? chartData.markers.filter((marker) => Number.isFinite(Number(marker?.time))) : []
  const newsCatalyst = selectedTradeDetail?.news_catalyst || null
  const mergedTradeMarkers = [
    ...tradeMarkers,
    ...((newsCatalyst?.news_markers || []).filter((marker) => Number.isFinite(Number(marker?.time)))),
  ]
  const priceLevels = Array.isArray(chartData?.price_levels) ? chartData.price_levels : []
  const moneyFlow = selectedTradeDetail?.money_flow || selectedTradeDetail?.market_context?.entry?.money_flow || null
  const unresolvedFills = overview?.unresolved_fills || []
  const summary = overview?.summary || {}
  const patterns = overview?.patterns || {}
  const improvement = overview?.improvement_plan || {}
  const filters = overview?.available_filters || {}
  const sync = overview?.sync || null
  const canSync = accountMode === 'ALL' || selectedAccountRefs.length > 0

  const applyFilters = async () => {
    setAppliedFilters({ ...draftFilters })
    await loadOverview({ ...draftFilters }, true)
  }

  const refreshAccounts = async () => {
    setBusy(true)
    setError('')
    try {
      await api.tradeReviewRefreshAccounts()
      await loadOverview(appliedFilters, false)
    } catch (e) {
      setError(e.message)
    } finally {
      setBusy(false)
    }
  }

  const saveSelection = async () => {
    setBusy(true)
    setError('')
    try {
      await api.tradeReviewSetSelection({
        selection_mode: accountMode,
        account_refs: selectedAccountRefs,
      })
      await loadOverview(appliedFilters, true)
    } catch (e) {
      setError(e.message)
    } finally {
      setBusy(false)
    }
  }

  const startSync = async () => {
    setSyncBusy(true)
    setError('')
    try {
      await api.tradeReviewStartSync({
        from_date: appliedFilters.from_date || null,
        to_date: appliedFilters.to_date || null,
        refresh_accounts: false,
      })
      await loadOverview(appliedFilters, true)
    } catch (e) {
      setError(e.message)
    } finally {
      setSyncBusy(false)
    }
  }

  const cancelSync = async () => {
    setSyncBusy(true)
    setError('')
    try {
      await api.tradeReviewCancel()
      await loadOverview(appliedFilters, true)
    } catch (e) {
      setError(e.message)
    } finally {
      setSyncBusy(false)
    }
  }

  const saveTradeNotes = async () => {
    if (!selectedTradeId) return
    setDetailLoading(true)
    setDetailError('')
    try {
      await api.tradeReviewUpdateTrade(selectedTradeId, {
        reviewed,
        admin_notes: adminNotes,
      })
      await loadDetail(selectedTradeId)
      await loadOverview(appliedFilters, true)
    } catch (e) {
      setDetailError(e.message)
    } finally {
      setDetailLoading(false)
    }
  }

  const toggleSelectedRef = (ref) => {
    setAccountMode('EXPLICIT')
    setSelectedAccountRefs((prev) => {
      if (prev.includes(ref)) {
        return prev.filter((item) => item !== ref)
      }
      return [...prev, ref]
    })
  }

  if (loading && !overview) {
    return <div className="card p-4">Loading trade review...</div>
  }

  if (currentUser?.role !== 'admin') {
    return <div className="card p-4 text-red-300">Admin access required.</div>
  }

  return (
    <div className="space-y-4">
      <div className="card p-4">
        <div className="flex flex-wrap items-start justify-between gap-3">
          <div>
            <h2 className="text-xl font-semibold">Trade Review</h2>
            <p className="mt-1 text-sm text-slate-400">Admin-only review of imported E*TRADE option trades, reconstructed fills, and deterministic coaching.</p>
          </div>
          <div className="flex flex-wrap items-center gap-2">
            <button className="rounded bg-slate-700 px-3 py-2 text-sm font-semibold text-slate-100 disabled:opacity-50" disabled={busy} onClick={refreshAccounts}>
              Refresh Accounts
            </button>
            <button className="rounded bg-accent px-3 py-2 text-sm font-semibold text-slate-900 disabled:opacity-50" disabled={busy || !canSync || syncBusy} onClick={saveSelection}>
              Save Selection
            </button>
            <button className="rounded bg-emerald-600 px-3 py-2 text-sm font-semibold text-slate-900 disabled:opacity-50" disabled={syncBusy || !canSync} onClick={startSync}>
              {syncBusy ? 'Syncing...' : 'Run Sync'}
            </button>
            <button className="rounded bg-red-900/50 px-3 py-2 text-sm font-semibold text-red-100 disabled:opacity-50" disabled={syncBusy || !(sync?.status === 'RUNNING' || sync?.status === 'PENDING' || sync?.status === 'FAILED')} onClick={cancelSync}>
              Cancel Sync
            </button>
          </div>
        </div>
        {(error || detailError) && <p className="mt-3 text-sm text-red-300">{error || detailError}</p>}
      </div>

      <div className="grid gap-3 md:grid-cols-2 xl:grid-cols-4">
        <SummaryStat label="Net P&L" value={money(summary.net_pnl)} tone={toneForPnl(summary.net_pnl)} />
        <SummaryStat label="Win Rate" value={pct(summary.win_rate)} />
        <SummaryStat label="Average Winner / Loser" value={`${money(summary.average_winner)} / ${money(summary.average_loser)}`} />
        <SummaryStat label="Profit Factor / Expectancy" value={`${summary.profit_factor ?? '-'} / ${money(summary.expectancy)}`} />
        <SummaryStat label="Max Drawdown" value={money(summary.max_drawdown)} tone="text-red-300" />
        <SummaryStat label="Strongest Edge" value={textOrDash(patterns.strongest_repeatable_edge)} />
        <SummaryStat label="Most Damaging Mistake" value={textOrDash(patterns.most_damaging_repeated_mistake)} />
        <SummaryStat label="Dollars Lost to Top Mistake" value={money(patterns.estimated_dollars_lost_by_top_mistake)} tone="text-red-300" />
        <SummaryStat label="Data Coverage" value={pct(summary.data_coverage)} />
        <SummaryStat label="Data Confidence" value={pct(summary.data_confidence_score)} />
        <SummaryStat label="Completed / Unresolved" value={`${summary.completed_trades ?? 0} / ${summary.unresolved_trades ?? 0}`} />
        <SummaryStat label="Trade Count" value={summary.total_trades ?? 0} />
      </div>

      <div className="card p-4">
        <div className="flex flex-wrap items-center justify-between gap-3">
          <div>
            <h3 className="text-lg font-semibold">Account Selector</h3>
            <p className="text-sm text-amber-300">Select the accounts to import and review. Imported records stay partitioned by account.</p>
          </div>
          <div className="flex items-center gap-4 text-sm">
            <label className="flex items-center gap-2">
              <input type="radio" checked={accountMode === 'EXPLICIT'} onChange={() => setAccountMode('EXPLICIT')} />
              Selected accounts
            </label>
            <label className="flex items-center gap-2">
              <input type="radio" checked={accountMode === 'ALL'} onChange={() => setAccountMode('ALL')} />
              All accounts
            </label>
          </div>
        </div>

        <div className="mt-3 grid gap-3 md:grid-cols-2 xl:grid-cols-3">
          {(overview?.accounts || []).map((account) => (
            <label key={account.account_ref} className={`card cursor-pointer p-3 transition ${account.selected || selectedAccountRefs.includes(account.account_ref) || accountMode === 'ALL' ? 'border-emerald-700/60 bg-emerald-900/10' : ''}`}>
              <div className="flex items-start justify-between gap-3">
                <div>
                  <div className="flex items-center gap-2">
                    <input
                      type="checkbox"
                      checked={accountMode === 'ALL' ? true : selectedAccountRefs.includes(account.account_ref)}
                      disabled={accountMode === 'ALL'}
                      onChange={() => toggleSelectedRef(account.account_ref)}
                    />
                    <span className="font-semibold text-slate-100">{account.account_mask || '-'}</span>
                  </div>
                  <p className="mt-1 text-xs text-slate-400">
                    {account.account_desc || '-'}{account.account_type ? ` • ${account.account_type}` : ''}{account.institution_type ? ` • ${account.institution_type}` : ''}
                  </p>
                </div>
                <span className={`rounded border px-2 py-0.5 text-[11px] font-semibold ${account.selected || selectedAccountRefs.includes(account.account_ref) || accountMode === 'ALL' ? 'border-emerald-700/60 bg-emerald-900/30 text-emerald-200' : 'border-slate-700 bg-panel2 text-slate-300'}`}>
                  {account.selected || accountMode === 'ALL' ? 'Selected' : 'Available'}
                </span>
              </div>
              <div className="mt-3 grid gap-1 text-xs text-slate-400">
                <div>Last sync: <span className="text-slate-200">{formatCentralTime(account.last_successful_sync_at)}</span></div>
                <div>Oldest history: <span className="text-slate-200">{formatCentralTime(account.oldest_available_history_at)}</span></div>
                <div>Sync status: <span className="text-slate-200">{account.last_sync_status || '-'}</span></div>
                <div>Trades / fills: <span className="text-slate-200">{account.trade_count ?? 0} / {account.fill_count ?? 0}</span></div>
              </div>
              {account.last_error_message && <p className="mt-2 text-xs text-amber-300">{account.last_error_message}</p>}
            </label>
          ))}
        </div>
      </div>

      <div className="grid gap-4 xl:grid-cols-[1.6fr_1fr]">
        <div className="space-y-4">
          <div className="card p-4">
            <div className="flex flex-wrap items-end gap-3">
              <div className="flex flex-col">
                <label className="text-[11px] uppercase text-slate-400">From</label>
                <input className="rounded border border-slate-700 bg-panel2 px-3 py-2 text-sm" type="date" value={draftFilters.from_date} onChange={(e) => setDraftFilters((prev) => ({ ...prev, from_date: e.target.value }))} />
              </div>
              <div className="flex flex-col">
                <label className="text-[11px] uppercase text-slate-400">To</label>
                <input className="rounded border border-slate-700 bg-panel2 px-3 py-2 text-sm" type="date" value={draftFilters.to_date} onChange={(e) => setDraftFilters((prev) => ({ ...prev, to_date: e.target.value }))} />
              </div>
              <div className="flex flex-col">
                <label className="text-[11px] uppercase text-slate-400">Ticker</label>
                <input className="rounded border border-slate-700 bg-panel2 px-3 py-2 text-sm" value={draftFilters.ticker} onChange={(e) => setDraftFilters((prev) => ({ ...prev, ticker: e.target.value }))} />
              </div>
              <div className="flex flex-col">
                <label className="text-[11px] uppercase text-slate-400">Call / Put</label>
                <select className="rounded border border-slate-700 bg-panel2 px-3 py-2 text-sm" value={draftFilters.call_put} onChange={(e) => setDraftFilters((prev) => ({ ...prev, call_put: e.target.value }))}>
                  <option value="">All</option>
                  <option value="CALL">CALL</option>
                  <option value="PUT">PUT</option>
                </select>
              </div>
              <div className="flex flex-col">
                <label className="text-[11px] uppercase text-slate-400">Winner / Loser</label>
                <select className="rounded border border-slate-700 bg-panel2 px-3 py-2 text-sm" value={draftFilters.winner_loser} onChange={(e) => setDraftFilters((prev) => ({ ...prev, winner_loser: e.target.value }))}>
                  <option value="">All</option>
                  <option value="WINNER">Winner</option>
                  <option value="LOSER">Loser</option>
                </select>
              </div>
              <div className="flex flex-col">
                <label className="text-[11px] uppercase text-slate-400">Grade</label>
                <select className="rounded border border-slate-700 bg-panel2 px-3 py-2 text-sm" value={draftFilters.grade} onChange={(e) => setDraftFilters((prev) => ({ ...prev, grade: e.target.value }))}>
                  <option value="">All</option>
                  {(filters.grades || []).map((grade) => <option key={grade} value={grade}>{grade}</option>)}
                </select>
              </div>
              <div className="flex flex-col">
                <label className="text-[11px] uppercase text-slate-400">DTE</label>
                <select className="rounded border border-slate-700 bg-panel2 px-3 py-2 text-sm" value={draftFilters.dte_bucket} onChange={(e) => setDraftFilters((prev) => ({ ...prev, dte_bucket: e.target.value }))}>
                  <option value="">All</option>
                  {(filters.dte_buckets || []).map((bucket) => <option key={bucket} value={bucket}>{bucket}</option>)}
                </select>
              </div>
              <div className="flex flex-col">
                <label className="text-[11px] uppercase text-slate-400">Setup</label>
                <select className="rounded border border-slate-700 bg-panel2 px-3 py-2 text-sm" value={draftFilters.setup_type} onChange={(e) => setDraftFilters((prev) => ({ ...prev, setup_type: e.target.value }))}>
                  <option value="">All</option>
                  {(filters.setup_types || []).map((setup) => <option key={setup} value={setup}>{setup}</option>)}
                </select>
              </div>
              <div className="flex flex-col">
                <label className="text-[11px] uppercase text-slate-400">Regime</label>
                <select className="rounded border border-slate-700 bg-panel2 px-3 py-2 text-sm" value={draftFilters.market_regime} onChange={(e) => setDraftFilters((prev) => ({ ...prev, market_regime: e.target.value }))}>
                  <option value="">All</option>
                  <option value="BULLISH">BULLISH</option>
                  <option value="BEARISH">BEARISH</option>
                  <option value="SIDEWAYS">SIDEWAYS</option>
                </select>
              </div>
              <div className="flex flex-col">
                <label className="text-[11px] uppercase text-slate-400">Reviewed</label>
                <select className="rounded border border-slate-700 bg-panel2 px-3 py-2 text-sm" value={draftFilters.reviewed} onChange={(e) => setDraftFilters((prev) => ({ ...prev, reviewed: e.target.value }))}>
                  <option value="">All</option>
                  <option value="true">Reviewed</option>
                  <option value="false">Unreviewed</option>
                </select>
              </div>
            </div>
            <div className="mt-3 flex flex-wrap gap-2">
              <button className="rounded bg-accent px-3 py-2 text-sm font-semibold text-slate-900" onClick={applyFilters}>Apply Filters</button>
              <button className="rounded bg-slate-700 px-3 py-2 text-sm font-semibold text-slate-100" onClick={() => setDraftFilters({ from_date: '', to_date: '', ticker: '', call_put: '', winner_loser: '', grade: '', dte_bucket: '', setup_type: '', market_regime: '', reviewed: '', limit: 200 })}>Reset Filters</button>
            </div>
          </div>

          <div className="card p-4">
            <div className="mb-3 flex items-center justify-between gap-3">
              <div>
                <h3 className="text-lg font-semibold">Trade Table</h3>
                <p className="text-sm text-slate-400">Completed trades and unresolved groups from the selected accounts.</p>
              </div>
              <div className="text-xs text-slate-400">
                Sync: <span className="font-semibold text-slate-200">{sync?.status || 'IDLE'}</span>
                {sync?.current_stage ? ` • ${sync.current_stage}` : ''}
              </div>
            </div>

            {sync?.current_message && <p className="mb-3 text-sm text-amber-300">{sync.current_message}</p>}
            {sync?.last_error && <p className="mb-3 text-sm text-red-300">{sync.last_error}</p>}

            <div className="overflow-auto">
              <table className="min-w-full text-xs">
                <thead>
                  <tr className="border-b border-slate-800 text-left text-slate-400">
                    {['Date', 'Account', 'Ticker', 'Contract', 'Side', 'Dir', 'Qty', 'DTE', 'Delta', 'Entry', 'Exit', 'Spread', 'Hold', 'P&L', 'Return %', 'Grade', 'Setup', 'Mistake', 'Status'].map((heading) => (
                      <th key={heading} className="px-2 py-2">{heading}</th>
                    ))}
                  </tr>
                </thead>
                <tbody>
                  {(overview?.trades || []).map((trade) => {
                    const active = trade.id === selectedTradeId
                    return (
                      <tr
                        key={trade.id}
                        className={`cursor-pointer border-t border-slate-800 hover:bg-slate-900/60 ${active ? 'bg-slate-900/80' : ''}`}
                        onClick={() => setSelectedTradeId(trade.id)}
                      >
                        <td className="px-2 py-2">{formatCentralTime(trade.closing_timestamp_utc || trade.opening_timestamp_utc, { seconds: false })}</td>
                        <td className="px-2 py-2">{trade.account_mask || '-'}</td>
                        <td className="px-2 py-2 font-semibold">{trade.underlying_symbol || '-'}</td>
                        <td className="px-2 py-2">{trade.occ_symbol || trade.option_symbol || '-'}</td>
                        <td className="px-2 py-2">{trade.call_put || '-'}</td>
                        <td className="px-2 py-2">{trade.direction || '-'}</td>
                        <td className="px-2 py-2">{trade.quantity ?? '-'}</td>
                        <td className="px-2 py-2">{trade.dte ?? '-'}</td>
                        <td className="px-2 py-2">{trade.delta ?? '-'}</td>
                        <td className="px-2 py-2">{money(trade.entry_price)}</td>
                        <td className="px-2 py-2">{money(trade.exit_price)}</td>
                        <td className="px-2 py-2">{pct(trade.entry_spread)}</td>
                        <td className="px-2 py-2">{secondsToText(trade.holding_time)}</td>
                        <td className={`px-2 py-2 font-semibold ${toneForPnl(trade.pnl)}`}>{money(trade.pnl)}</td>
                        <td className="px-2 py-2">{pct(trade.return_pct)}</td>
                        <td className="px-2 py-2"><span className={`rounded border px-2 py-0.5 text-[11px] font-semibold ${toneForGrade(trade.grade)}`}>{trade.grade || '-'}</span></td>
                        <td className="px-2 py-2">{trade.setup || '-'}</td>
                        <td className="px-2 py-2">{textOrDash(trade.primary_mistake)}</td>
                        <td className="px-2 py-2">{trade.analysis_status || '-'}</td>
                      </tr>
                    )
                  })}
                  {!overview?.trades?.length && (
                    <tr>
                      <td colSpan="19" className="px-2 py-4 text-slate-400">No trades matched the current filters.</td>
                    </tr>
                  )}
                </tbody>
              </table>
            </div>
          </div>

          <div className="grid gap-4 lg:grid-cols-2">
            <div className="card p-4">
              <h3 className="mb-3 text-lg font-semibold">Pattern Analysis</h3>
              <div className="grid gap-4 md:grid-cols-2">
                <div>
                  <p className="mb-2 text-[11px] font-semibold uppercase text-slate-400">Top Strengths</p>
                  <List items={(patterns.top_strengths || []).map((row) => `${row.name} (${row.trades} trades, ${money(row.estimated_dollars)})`)} />
                </div>
                <div>
                  <p className="mb-2 text-[11px] font-semibold uppercase text-slate-400">Top Mistakes</p>
                  <List items={(patterns.top_mistakes || []).map((row) => `${row.name} (${row.trades} trades, ${money(row.estimated_dollars_lost)})`)} />
                </div>
                <div>
                  <p className="mb-2 text-[11px] font-semibold uppercase text-slate-400">Best Conditions</p>
                  <List items={[
                    patterns.best_trading_conditions?.ticker ? `Ticker bucket: ${patterns.best_trading_conditions.ticker}` : null,
                    patterns.best_trading_conditions?.setup ? `Setup bucket: ${patterns.best_trading_conditions.setup}` : null,
                  ]} />
                </div>
                <div>
                  <p className="mb-2 text-[11px] font-semibold uppercase text-slate-400">Worst Conditions</p>
                  <List items={[
                    patterns.worst_trading_conditions?.ticker ? `Ticker bucket: ${patterns.worst_trading_conditions.ticker}` : null,
                    patterns.worst_trading_conditions?.mistake ? `Mistake: ${patterns.worst_trading_conditions.mistake}` : null,
                  ]} />
                </div>
                <div>
                  <p className="mb-2 text-[11px] font-semibold uppercase text-slate-400">Best Contract Profile</p>
                  <List items={[
                    patterns.best_contract_profile?.dte_bucket ? `DTE bucket: ${patterns.best_contract_profile.dte_bucket}` : null,
                    patterns.best_contract_profile?.delta_bucket ? `Delta bucket: ${patterns.best_contract_profile.delta_bucket}` : null,
                  ]} />
                </div>
                <div>
                  <p className="mb-2 text-[11px] font-semibold uppercase text-slate-400">Worst Contract Profile</p>
                  <List items={[
                    patterns.worst_contract_profile?.spread_bucket ? `Spread bucket: ${patterns.worst_contract_profile.spread_bucket}` : null,
                    patterns.worst_contract_profile?.volume_bucket ? `Volume bucket: ${patterns.worst_contract_profile.volume_bucket}` : null,
                  ]} />
                </div>
              </div>
              <div className="mt-4">
                <p className="mb-2 text-[11px] font-semibold uppercase text-slate-400">Rules That Would Have Prevented Losses</p>
                <List items={patterns.rules_that_would_have_prevented_losses || []} />
              </div>
              <div className="mt-4 rounded border border-slate-700 bg-slate-900/40 p-3">
                <p className="mb-2 text-[11px] font-semibold uppercase text-slate-400">Money Flow Stats</p>
                <div className="grid gap-2 text-sm sm:grid-cols-2">
                  <div>Win rate aligned: <span className="font-semibold">{pct(patterns.money_flow_stats?.win_rate_when_aligned_with_money_flow)}</span></div>
                  <div>Win rate against: <span className="font-semibold">{pct(patterns.money_flow_stats?.win_rate_when_against_money_flow)}</span></div>
                  <div>Avg P/L aligned: <span className="font-semibold">{money(patterns.money_flow_stats?.average_pnl_when_aligned_with_money_flow)}</span></div>
                  <div>Avg P/L against: <span className="font-semibold">{money(patterns.money_flow_stats?.average_pnl_when_against_money_flow)}</span></div>
                  <div>Above VWAP: <span className="font-semibold">{money(patterns.money_flow_stats?.performance_above_vwap)}</span></div>
                  <div>Below VWAP: <span className="font-semibold">{money(patterns.money_flow_stats?.performance_below_vwap)}</span></div>
                  <div>Options confirmed: <span className="font-semibold">{money(patterns.money_flow_stats?.performance_when_options_positioning_confirmed)}</span></div>
                  <div>Options conflicted: <span className="font-semibold">{money(patterns.money_flow_stats?.performance_when_options_positioning_conflicted)}</span></div>
                </div>
              </div>
            </div>

            <div className="card p-4">
              <h3 className="mb-3 text-lg font-semibold">Improvement Plan</h3>
              <div className="grid gap-3 text-sm sm:grid-cols-2">
                <div>Max risk per trade: <span className="font-semibold">{money(improvement.maximum_risk_per_trade)}</span></div>
                <div>Max daily loss: <span className="font-semibold">{money(improvement.maximum_daily_loss)}</span></div>
                <div>Minimum reward/risk: <span className="font-semibold">{improvement.minimum_reward_to_risk ?? '-'}</span></div>
                <div>Preferred DTE: <span className="font-semibold">{improvement.preferred_dte_range || '-'}</span></div>
                <div>Preferred delta: <span className="font-semibold">{improvement.preferred_delta_range || '-'}</span></div>
                <div>Max spread: <span className="font-semibold">{improvement.maximum_acceptable_bid_ask_spread || '-'}</span></div>
                <div>Minimum volume / OI: <span className="font-semibold">{improvement.minimum_volume_and_open_interest || '-'}</span></div>
                <div>Max simultaneous positions: <span className="font-semibold">{improvement.maximum_simultaneous_positions ?? '-'}</span></div>
              </div>
              <div className="mt-4 grid gap-4 md:grid-cols-2">
                <div>
                  <p className="mb-2 text-[11px] font-semibold uppercase text-slate-400">Confirmation</p>
                  <List items={improvement.confirmation_requirements || []} />
                </div>
                <div>
                  <p className="mb-2 text-[11px] font-semibold uppercase text-slate-400">Against Averaging Down</p>
                  <List items={improvement.rules_against_averaging_down || []} />
                </div>
                <div>
                  <p className="mb-2 text-[11px] font-semibold uppercase text-slate-400">Pre-Trade Checklist</p>
                  <List items={improvement.pre_trade_checklist || []} />
                </div>
                <div>
                  <p className="mb-2 text-[11px] font-semibold uppercase text-slate-400">Post-Trade Checklist</p>
                  <List items={improvement.post_trade_review_checklist || []} />
                </div>
              </div>
            </div>
          </div>

          <div className="card p-4">
            <h3 className="mb-3 text-lg font-semibold">Unresolved Fills Review Queue</h3>
            <div className="grid gap-2 md:grid-cols-2 xl:grid-cols-3">
              {unresolvedFills.map((fill) => (
                <div key={fill.id} className="rounded border border-amber-800/60 bg-amber-900/20 p-3 text-sm text-amber-100">
                  <div className="font-semibold">{fill.account_mask || '-'} • {fill.underlying_symbol || fill.symbol || '-'}</div>
                  <div className="text-xs text-amber-200">{fill.source_type} • {fill.action || '-'} • {fill.quantity ?? '-'} @ {money(fill.fill_price)}</div>
                  <div className="text-xs text-slate-300">{formatCentralTime(fill.timestamp, { seconds: false })}</div>
                </div>
              ))}
              {!unresolvedFills.length && <p className="text-sm text-slate-400">No unresolved fills were found.</p>}
            </div>
          </div>
        </div>

        <div className="space-y-4">
          <div className="card p-4">
            <div className="flex items-start justify-between gap-3">
              <div>
                <h3 className="text-lg font-semibold">Trade Detail</h3>
                <p className="text-sm text-slate-400">{selectedTrade ? `${selectedTrade.underlying_symbol || '-'} ${selectedTrade.occ_symbol || ''}` : 'Select a trade to inspect fills, chart levels, and coaching.'}</p>
              </div>
              {selectedTrade && (
                <span className={`rounded border px-2 py-0.5 text-[11px] font-semibold ${toneForGrade(selectedTrade.grade)}`}>{selectedTrade.grade || '-'}</span>
              )}
            </div>

            {detailLoading && <p className="mt-3 text-sm text-slate-400">Loading detail...</p>}
            {!detailLoading && detail && (
              <div className="mt-3 space-y-4">
                <div className="rounded border border-slate-700 bg-panel2 p-3 text-sm">
                  <p className="font-semibold text-slate-100">{detail.trade?.hard_truth || 'No detail available.'}</p>
                  <p className="mt-2 text-slate-300">{detail.trade?.lesson || '-'}</p>
                </div>

                <TickerChart indicatorData={chartData} tradeMarkers={mergedTradeMarkers} priceLevels={priceLevels} />
                <MoneyFlowPanel moneyFlow={moneyFlow} title="Money Flow at Entry" compact={false} />
                <NewsCatalystPanel newsCatalyst={newsCatalyst} />

                <div className="grid gap-3 sm:grid-cols-2">
                  <div>
                    <p className="mb-1 text-[11px] font-semibold uppercase text-slate-400">What Went Well</p>
                    <List items={detail.trade?.what_went_well} />
                  </div>
                  <div>
                    <p className="mb-1 text-[11px] font-semibold uppercase text-slate-400">What Went Poorly</p>
                    <List items={detail.trade?.what_went_poorly} />
                  </div>
                  <div>
                    <p className="mb-1 text-[11px] font-semibold uppercase text-slate-400">Hard Truth</p>
                    <p className="text-sm text-slate-200">{detail.trade?.hard_truth || '-'}</p>
                  </div>
                  <div>
                    <p className="mb-1 text-[11px] font-semibold uppercase text-slate-400">Recommended Alternative</p>
                    <List items={[
                      detail.trade?.better_entry ? `Better entry: ${detail.trade.better_entry}` : null,
                      detail.trade?.better_invalidation ? `Better invalidation: ${detail.trade.better_invalidation}` : null,
                      detail.trade?.better_exit_plan ? `Better exit: ${detail.trade.better_exit_plan}` : null,
                    ]} />
                  </div>
                </div>

                <div className="grid gap-3 text-sm sm:grid-cols-2">
                  <div>Support: <span className="font-semibold">{money(detail.trade?.levels?.support)}</span></div>
                  <div>Resistance: <span className="font-semibold">{money(detail.trade?.levels?.resistance)}</span></div>
                  <div>Stop: <span className="font-semibold">{money(detail.trade?.levels?.stop)}</span></div>
                  <div>Targets: <span className="font-semibold">{money(detail.trade?.levels?.target_1)} / {money(detail.trade?.levels?.target_2)} / {money(detail.trade?.levels?.stretch_target)}</span></div>
                  <div>MFE / MAE: <span className="font-semibold">{money(detail.trade?.mfe)} / {money(detail.trade?.mae)}</span></div>
                  <div>Data confidence: <span className="font-semibold">{detail.trade?.data_confidence_label || '-'}</span></div>
                </div>

                <div className="grid gap-3 sm:grid-cols-2">
                  <div>
                    <p className="mb-1 text-[11px] font-semibold uppercase text-slate-400">Entry / Exit Fills</p>
                    <div className="space-y-2">
                      {(detail.trade?.fills || []).map((fill) => (
                        <div key={fill.id} className="rounded border border-slate-700 bg-panel2 p-2 text-xs text-slate-300">
                          <div className="flex items-center justify-between gap-2">
                            <span className="font-semibold text-slate-100">{fill.action || '-'}</span>
                            <span>{formatCentralTime(fill.execution_timestamp_utc, { seconds: false })}</span>
                          </div>
                          <div>{fill.quantity ?? '-'} @ {money(fill.fill_price)} | Commission {money(fill.commission)} | Fees {money(fill.fees)}</div>
                          <div>Match: {fill.match_status || '-'}</div>
                        </div>
                      ))}
                    </div>
                  </div>
                  <div>
                    <p className="mb-1 text-[11px] font-semibold uppercase text-slate-400">Option Metrics</p>
                    <div className="space-y-1 text-xs text-slate-300">
                      <div>Call / Put: <span className="font-semibold">{detail.trade?.call_put || '-'}</span></div>
                      <div>Strike: <span className="font-semibold">{money(detail.trade?.strike)}</span></div>
                      <div>Expiration: <span className="font-semibold">{detail.trade?.expiration || '-'}</span></div>
                      <div>DTE: <span className="font-semibold">{detail.trade?.dte ?? '-'}</span></div>
                      <div>Direction: <span className="font-semibold">{detail.trade?.direction || '-'}</span></div>
                      <div>Grade breakdown: <span className="font-semibold">{textOrDash(detail.trade?.grade_breakdown)}</span></div>
                      <div>Setup tags: <span className="font-semibold">{textOrDash(detail.trade?.pattern_tags)}</span></div>
                    </div>
                  </div>
                </div>

                <div className="rounded border border-slate-700 bg-panel2 p-3">
                  <p className="mb-2 text-[11px] font-semibold uppercase text-slate-400">Historical Values</p>
                  <div className="grid gap-2 text-xs text-slate-300 sm:grid-cols-2">
                    <div>Entry source: <span className="font-semibold">{detail.trade?.market_context?.entry?.source || '-'}</span></div>
                    <div>Entry confidence: <span className="font-semibold">{detail.trade?.market_context?.entry?.confidence || '-'}</span></div>
                    <div>Exit source: <span className="font-semibold">{detail.trade?.market_context?.exit?.source || '-'}</span></div>
                    <div>Exit confidence: <span className="font-semibold">{detail.trade?.market_context?.exit?.confidence || '-'}</span></div>
                    <div>VWAP: <span className="font-semibold">{money(detail.trade?.market_context?.entry?.value?.vwap)}</span></div>
                    <div>EMA 9 / 21 / 50 / 200: <span className="font-semibold">{money(detail.trade?.market_context?.entry?.value?.ema_9)} / {money(detail.trade?.market_context?.entry?.value?.ema_21)} / {money(detail.trade?.market_context?.entry?.value?.ema_50)} / {money(detail.trade?.market_context?.entry?.value?.ema_200)}</span></div>
                    <div>Volume: <span className="font-semibold">{detail.trade?.market_context?.entry?.value?.underlying_candle?.volume ?? '-'}</span></div>
                    <div>Entry above VWAP: <span className="font-semibold">{String(detail.trade?.market_context?.entry?.value?.entry_above_vwap ?? '-')}</span></div>
                  </div>
                </div>

                <div className="rounded border border-slate-700 bg-panel2 p-3">
                  <p className="mb-2 text-[11px] font-semibold uppercase text-slate-400">GPT Coaching</p>
                  {detail.analysis?.status === 'ok' ? (
                    <div className="space-y-2 text-sm text-slate-200">
                      <p className="font-semibold text-slate-100">{detail.analysis.analysis?.headline || '-'}</p>
                      <List items={detail.analysis.analysis?.what_went_well} />
                      <List items={detail.analysis.analysis?.what_went_poorly} />
                      <p className="text-slate-300">{detail.analysis.analysis?.hard_truth || '-'}</p>
                      <p className="text-slate-400">Lesson: {detail.analysis.analysis?.single_most_important_lesson || '-'}</p>
                      <p className="text-slate-400">Metrics used: {textOrDash(detail.analysis.analysis?.metrics_used)}</p>
                    </div>
                  ) : (
                    <p className="text-sm text-amber-300">{detail.analysis?.blocking_reason || 'Trade coaching is unavailable.'}</p>
                  )}
                </div>

                <div className="rounded border border-slate-700 bg-panel2 p-3">
                  <p className="mb-2 text-[11px] font-semibold uppercase text-slate-400">Admin Notes</p>
                  <textarea
                    className="min-h-24 w-full rounded border border-slate-700 bg-slate-950 px-3 py-2 text-sm text-slate-100"
                    value={adminNotes}
                    onChange={(e) => setAdminNotes(e.target.value)}
                  />
                  <div className="mt-2 flex items-center justify-between gap-2">
                    <label className="flex items-center gap-2 text-sm text-slate-300">
                      <input type="checkbox" checked={reviewed} onChange={(e) => setReviewed(e.target.checked)} />
                      Mark reviewed
                    </label>
                    <button className="rounded bg-accent px-3 py-2 text-sm font-semibold text-slate-900" onClick={saveTradeNotes}>
                      Save Notes
                    </button>
                  </div>
                </div>
              </div>
            )}
            {!detailLoading && !detail && <p className="mt-3 text-sm text-slate-400">Select a trade from the table to see the detail panel.</p>}
          </div>
        </div>
      </div>
    </div>
  )
}
