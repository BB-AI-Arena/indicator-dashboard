import { useEffect, useState } from 'react'
import { api } from './api'
import Dashboard from './components/Dashboard'
import TickerDetail from './components/TickerDetail'
import Alerts from './components/Alerts'
import Settings from './components/Settings'
import MarketStatusBanner from './components/MarketStatusBanner'
import NewsTicker from './components/NewsTicker'
import EarningsTicker from './components/EarningsTicker'
import LoginPage from './components/LoginPage'
import EtradePositions from './components/EtradePositions'
import PaperPortfolio from './components/PaperPortfolio'
import TradeReview from './components/TradeReview'
import WatchlistIntelligence from './components/WatchlistIntelligence'

function brokerStatusLabel(status, error) {
  if (error) return 'Status check failed'
  if (!status) return 'Checking'
  if (!status.enabled) return 'Disabled'
  if (!status.configured) return 'Credentials missing'
  if (!status.connected) return 'Disconnected'
  return 'Connected'
}

function brokerStatusTone(status, error) {
  if (!status && !error) return 'status-chip-warn'
  if (error || !status.enabled || !status.configured || !status.connected) return 'status-chip-danger'
  return 'status-chip-ok'
}

function EtradeDisconnectAlert({ status, error, onSettings }) {
  const message = error || status?.message || 'E*TRADE is not connected'
  return (
    <div className="broker-disconnect-alert" role="alert">
      <div>
        <div className="alert-kicker">Broker Connection Required</div>
        <div className="alert-title">E*TRADE DISCONNECTED</div>
      </div>
      <div className="alert-copy">
        {message}. Position data, option chains, and account-aware trade review need a fresh E*TRADE connection.
      </div>
      <button className="alert-action" onClick={onSettings}>Open Settings</button>
    </div>
  )
}

export default function App() {
  const [activeTab, setActiveTab] = useState('Dashboard')
  const [selectedSymbol, setSelectedSymbol] = useState('SPY')
  const [health, setHealth] = useState(null)
  const [marketSession, setMarketSession] = useState(null)
  const [etradeStatus, setEtradeStatus] = useState(null)
  const [etradeStatusError, setEtradeStatusError] = useState('')
  const [auth, setAuth] = useState({ loading: true, authenticated: false, setupRequired: false, mustChangePassword: false, blockedReason: '', user: null })

  useEffect(() => {
    api.health().then(setHealth).catch(() => setHealth(null))
  }, [])

  useEffect(() => {
    let mounted = true
    const loadSession = async () => {
      try {
        const session = await api.marketSession()
        if (mounted) setMarketSession(session)
      } catch {
        if (mounted) setMarketSession(null)
      }
    }
    loadSession()
    const id = setInterval(loadSession, 30000)
    return () => {
      mounted = false
      clearInterval(id)
    }
  }, [])

  useEffect(() => {
    let mounted = true
    const loadAuth = async () => {
      try {
        const status = await api.authStatus()
        if (!mounted) return
        setAuth({
          loading: false,
          authenticated: Boolean(status.authenticated),
          setupRequired: Boolean(status.setup_required),
          mustChangePassword: Boolean(status.user?.must_change_password),
          blockedReason: status.ip_blocked ? status.ip_block_reason || 'Access blocked' : '',
          user: status.user || null,
        })
        if (status.user?.username) {
          setActiveTab('Dashboard')
        }
      } catch (e) {
        if (!mounted) return
        setAuth({
          loading: false,
          authenticated: false,
          setupRequired: false,
          mustChangePassword: false,
          blockedReason: e.message || 'Access blocked',
          user: null,
        })
      }
    }
    loadAuth()
    return () => {
      mounted = false
    }
  }, [])

  useEffect(() => {
    if (!auth.authenticated || auth.user?.role !== 'admin') {
      setEtradeStatus(null)
      setEtradeStatusError('')
      return undefined
    }
    let mounted = true
    const loadStatus = async () => {
      try {
        const status = await api.etradeStatus()
        if (!mounted) return
        setEtradeStatus(status)
        setEtradeStatusError('')
      } catch (e) {
        if (!mounted) return
        setEtradeStatus(null)
        setEtradeStatusError(e.message || 'Unable to check E*TRADE status')
      }
    }
    loadStatus()
    const id = setInterval(loadStatus, 30000)
    return () => {
      mounted = false
      clearInterval(id)
    }
  }, [auth.authenticated, auth.user?.role])

  const onAuthenticated = (session) => {
    setAuth((prev) => ({
      ...prev,
      loading: false,
      authenticated: true,
      setupRequired: false,
      mustChangePassword: Boolean(session?.user?.must_change_password),
      blockedReason: '',
      user: session?.user || prev.user,
    }))
    setActiveTab('Dashboard')
  }

  const onPasswordChanged = (user) => {
    setAuth((prev) => ({
      ...prev,
      loading: false,
      authenticated: true,
      mustChangePassword: false,
      blockedReason: '',
      user: user || prev.user,
    }))
    setActiveTab('Dashboard')
  }

  const logout = async () => {
    try {
      await api.authLogout()
    } catch {
      // ignore
    } finally {
      api.clearAuthToken()
      setAuth({ loading: false, authenticated: false, setupRequired: false, mustChangePassword: false, blockedReason: '', user: null })
      setActiveTab('Dashboard')
    }
  }

  const tabs = auth.user?.role === 'admin'
    ? ['Dashboard', 'Watchlist Intelligence', 'Ticker Detail', 'E*TRADE Positions', 'Trade Review', 'Paper Portfolio', 'Admin', 'Alerts']
    : ['Dashboard', 'Watchlist Intelligence', 'Ticker Detail', 'Alerts', 'Settings']

  const isAdmin = auth.user?.role === 'admin'
  const showEtradeAlert = isAdmin && (Boolean(etradeStatusError) || (etradeStatus && (!etradeStatus.enabled || !etradeStatus.configured || !etradeStatus.connected)))
  const apiLabel = health?.status === 'ok' ? 'Online' : 'Checking'
  const apiTone = health?.status === 'ok' ? 'status-chip-ok' : 'status-chip-warn'
  const sessionLabel = marketSession?.actionable_live_quotes ? 'Live session' : 'Planning mode'
  const sessionTone = marketSession?.actionable_live_quotes ? 'status-chip-ok' : 'status-chip-danger'

  if (auth.loading) {
    return <div className="app-shell min-h-screen p-4 md:p-6"><div className="access-panel mx-auto max-w-7xl card p-6 text-slate-300">Checking access...</div></div>
  }

  if (!auth.authenticated) {
    return (
      <LoginPage
        setupRequired={auth.setupRequired}
        blockedReason={auth.blockedReason}
        onAuthenticated={onAuthenticated}
      />
    )
  }

  if (auth.mustChangePassword) {
    return (
      <LoginPage
        mustChangePassword
        currentUser={auth.user}
        onPasswordChanged={onPasswordChanged}
      />
    )
  }

  return (
    <div className="app-shell min-h-screen">
      <div className="mx-auto max-w-[1500px] px-3 py-4 md:px-6">
        <MarketStatusBanner marketSession={marketSession} />
        {showEtradeAlert && (
          <EtradeDisconnectAlert
          status={etradeStatus}
            error={etradeStatusError}
            onSettings={() => setActiveTab(isAdmin ? 'Admin' : 'Settings')}
          />
        )}
        <NewsTicker />
        <EarningsTicker />
        <header className="command-header mb-4">
          <div className="command-header-main">
            <div className="brand-stack">
              <div className="eyebrow">Options Trading Platform</div>
              <h1>Indicator Command Center</h1>
              <p>Scanner, broker positions, market session, catalyst context, and risk controls.</p>
            </div>
            <div className="command-status-grid">
              <div className={`status-chip ${apiTone}`}>
                <span>API</span>
                <strong>{apiLabel}</strong>
                <small>{api.baseUrl || 'same-origin'}</small>
              </div>
              <div className={`status-chip ${sessionTone}`}>
                <span>Session</span>
                <strong>{sessionLabel}</strong>
                <small>{marketSession?.session_state || 'Loading'}</small>
              </div>
              {isAdmin && (
                <div className={`status-chip ${brokerStatusTone(etradeStatus, etradeStatusError)}`}>
                  <span>E*TRADE</span>
                  <strong>{brokerStatusLabel(etradeStatus, etradeStatusError)}</strong>
                  <small>{etradeStatus?.sandbox ? 'Sandbox' : 'Live broker'}</small>
                </div>
              )}
              <div className="status-chip status-chip-user">
                <span>User</span>
                <strong>{auth.user?.username || 'unknown'}</strong>
                <small>{auth.user?.role || 'user'}</small>
              </div>
            </div>
          </div>
          <div className="command-actions">
            <button className="logout-button" onClick={logout}>Logout</button>
          </div>
          <nav className="tab-rail" aria-label="Primary dashboard sections">
            {tabs.map((tab) => (
              <button
                key={tab}
                className={`nav-tab ${activeTab === tab ? 'nav-tab-active' : ''}`}
                onClick={() => setActiveTab(tab)}
              >
                {tab}
              </button>
            ))}
          </nav>
        </header>

        {activeTab === 'Dashboard' && <Dashboard onSelectSymbol={(s) => { setSelectedSymbol(s); setActiveTab('Ticker Detail') }} currentUser={auth.user} marketSession={marketSession} />}
        {activeTab === 'Watchlist Intelligence' && <WatchlistIntelligence onSelectSymbol={(s) => { setSelectedSymbol(s); setActiveTab('Ticker Detail') }} />}
        {activeTab === 'Ticker Detail' && <TickerDetail symbol={selectedSymbol} onSymbolChange={setSelectedSymbol} marketSession={marketSession} currentUser={auth.user} />}
        {activeTab === 'E*TRADE Positions' && auth.user?.role === 'admin' && <EtradePositions currentUser={auth.user} marketSession={marketSession} />}
        {activeTab === 'Paper Portfolio' && auth.user?.role === 'admin' && <PaperPortfolio />}
        {activeTab === 'Trade Review' && auth.user?.role === 'admin' && <TradeReview currentUser={auth.user} />}
        {activeTab === 'Alerts' && <Alerts />}
        {activeTab === 'Admin' && auth.user?.role === 'admin' && <Settings currentUser={auth.user} />}
        {activeTab === 'Settings' && auth.user?.role !== 'admin' && <Settings currentUser={auth.user} />}
      </div>
    </div>
  )
}
