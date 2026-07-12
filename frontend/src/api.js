const API_BASE_URL = (import.meta.env.VITE_API_BASE_URL || '').trim()
const AUTH_TOKEN_KEY = 'indicator-dashboard-auth-token'

function getAuthToken() {
  if (typeof window === 'undefined') return ''
  try {
    return window.localStorage.getItem(AUTH_TOKEN_KEY) || ''
  } catch {
    return ''
  }
}

function setAuthToken(token) {
  if (typeof window === 'undefined') return
  try {
    if (token) window.localStorage.setItem(AUTH_TOKEN_KEY, token)
    else window.localStorage.removeItem(AUTH_TOKEN_KEY)
  } catch {
    // ignore
  }
}

async function request(path, options = {}) {
  const { timeoutMs, authToken, ...fetchOptions } = options
  const headers = new Headers(fetchOptions.headers || {})
  const token = authToken || getAuthToken()
  if (token && !headers.has('Authorization')) {
    headers.set('Authorization', `Bearer ${token}`)
  }
  let timeoutId = null
  const controller = timeoutMs ? new AbortController() : null
  if (controller) {
    fetchOptions.signal = controller.signal
    timeoutId = setTimeout(() => controller.abort(), timeoutMs)
  }
  try {
    const res = await fetch(`${API_BASE_URL}${path}`, {
      ...fetchOptions,
      headers,
    })
    if (!res.ok) {
      let detail = res.status === 504
        ? `Request timed out while loading ${path}`
        : `Request failed (${res.status})`
      try {
        const body = await res.json()
        detail = body.detail || detail
      } catch {
        // ignore
      }
      if (res.status === 401) {
        setAuthToken('')
      }
      throw new Error(detail)
    }
    return res.json()
  } catch (error) {
    if (controller?.signal?.aborted) {
      throw new Error(`Request timed out after ${Math.ceil((timeoutMs || 0) / 1000)}s: ${path}`)
    }
    throw error
  } finally {
    if (timeoutId) clearTimeout(timeoutId)
  }
}

export const api = {
  baseUrl: API_BASE_URL,
  getAuthToken,
  setAuthToken,
  clearAuthToken: () => setAuthToken(''),
  health: () => request('/api/health'),
  authStatus: () => request('/api/auth/status', { timeoutMs: 5000 }),
  authMe: () => request('/api/auth/me', { timeoutMs: 5000 }),
  authLogin: (payload) => request('/api/auth/login', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload),
  }),
  authLogout: () => request('/api/auth/logout', { method: 'POST' }),
  authChangePassword: (payload, authToken = '') => request('/api/auth/change-password', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload),
    authToken,
  }),
  authBootstrap: (payload) => request('/api/auth/bootstrap', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload),
  }),
  authUsers: () => request('/api/auth/users'),
  authCreateUser: (payload) => request('/api/auth/users', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload),
  }),
  authUpdateUser: (username, payload) => request(`/api/auth/users/${encodeURIComponent(username)}`, {
    method: 'PATCH',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload),
  }),
  authBlockedIps: () => request('/api/auth/blocked-ips'),
  authUnblockIp: (ip) => request(`/api/auth/blocked-ips/${encodeURIComponent(ip)}`, { method: 'DELETE' }),
  config: () => request('/api/config'),
  decisionDashboard: () => request('/api/dashboard/decision', { timeoutMs: 10000 }),
  activeSignals: (refresh = false) => request(`/api/signals/active${refresh ? '?refresh=true' : ''}`, { timeoutMs: 20000 }),
  refreshActiveSignals: () => request('/api/signals/refresh', { method: 'POST', timeoutMs: 30000 }),
  triggerActiveSignal: (signalId, payload = {}) => request(`/api/signals/${encodeURIComponent(signalId)}/trigger`, {
    method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(payload), timeoutMs: 15000,
  }),
  signalHistory: (limit = 100) => request(`/api/signals/history?limit=${encodeURIComponent(limit)}`, { timeoutMs: 15000 }),
  activeSignalStatus: () => request('/api/signals/status', { timeoutMs: 10000 }),
  recommendationPerformance: () => request('/api/recommendations/performance', { timeoutMs: 10000 }),
  recommendations: (limit = 100) => request(`/api/recommendations?limit=${encodeURIComponent(limit)}`, { timeoutMs: 10000 }),
  triggerRecommendation: (id, payload = {}) => request(`/api/recommendations/${encodeURIComponent(id)}/trigger`, {
    method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(payload), timeoutMs: 10000,
  }),
  resolveRecommendation: (id, payload = {}) => request(`/api/recommendations/${encodeURIComponent(id)}/resolve`, {
    method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(payload), timeoutMs: 10000,
  }),
  watchlistIntelligence: () => request('/api/watchlist/intelligence', { timeoutMs: 10000 }),
  tickerProfile: (symbol, refresh = false) => request(`/api/ticker-profiles/${encodeURIComponent(symbol)}${refresh ? '?refresh=true' : ''}`, { timeoutMs: 25000 }),
  refreshTickerProfile: (symbol) => request(`/api/ticker-profiles/${encodeURIComponent(symbol)}/refresh`, { method: 'POST', timeoutMs: 15000 }),
  advisorySettings: () => request('/api/admin/advisory/settings', { timeoutMs: 10000 }),
  updateAdvisorySettings: (payload) => request('/api/admin/advisory/settings', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload),
    timeoutMs: 10000,
  }),
  advisoryForSymbol: (symbol, payload = {}) => request(`/api/advisory/${encodeURIComponent(symbol)}`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload),
    timeoutMs: 60000,
  }),
  socialSettings: () => request('/api/admin/social/settings', { timeoutMs: 10000 }),
  updateSocialSettings: (payload) => request('/api/admin/social/settings', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload),
    timeoutMs: 10000,
  }),
  marketNews: () => request('/api/news/rss', { timeoutMs: 15000 }),
  earningsNews: () => request('/api/news/earnings', { timeoutMs: 15000 }),
  newsCatalystImpact: (symbol, params = {}) => {
    const query = new URLSearchParams()
    Object.entries(params || {}).forEach(([key, value]) => {
      if (value === undefined || value === null || value === '') return
      query.set(key, String(value))
    })
    const suffix = query.toString() ? `?${query.toString()}` : ''
    return request(`/api/news/catalyst/${encodeURIComponent(symbol)}${suffix}`, { timeoutMs: 25000 })
  },
  dbStatus: () => request('/api/db/status'),
  providersStatus: () => request('/api/providers/status'),
  marketSession: () => request('/api/market/session'),
  quote: (symbol) => request(`/api/quote/${symbol}`, { timeoutMs: 10000 }),
  backfillStatus: () => request('/api/history/backfill/status'),
  startBackfill: (payload = {}) => request('/api/history/backfill', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload),
  }),
  historicalSetupMatch: (symbol, params = {}) => {
    const query = new URLSearchParams()
    Object.entries(params || {}).forEach(([key, value]) => {
      if (value === undefined || value === null || value === '') return
      query.set(key, String(value))
    })
    const suffix = query.toString() ? `?${query.toString()}` : ''
    return request(`/api/history/setup-match/${encodeURIComponent(symbol)}${suffix}`, { timeoutMs: 45000 })
  },
  startSetupMatchBackfill: (payload = {}) => request('/api/history/setup-match/backfill', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload),
    timeoutMs: 10000,
  }),
  cancelBackfill: () => request('/api/history/backfill/cancel', { method: 'POST' }),
  watchlist: () => request('/api/watchlist', { timeoutMs: 10000 }),
  addWatchlist: (symbol) => request('/api/watchlist', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ symbol }),
  }),
  removeWatchlist: (symbol) => request(`/api/watchlist/${symbol}`, { method: 'DELETE' }),
  scan: () => request('/api/scan', { timeoutMs: 15000 }),
  scanSymbol: (symbol) => request(`/api/scan/${symbol}`, { timeoutMs: 15000 }),
  runScan: () => request('/api/scan/run', { method: 'POST', timeoutMs: 10000 }),
  indicators: (symbol, interval = '5m', period = '5d') =>
    request(`/api/indicators/${symbol}?interval=${encodeURIComponent(interval)}&period=${encodeURIComponent(period)}`, { timeoutMs: 15000 }),
  optionsRatios: (symbol) => request(`/api/options/${symbol}/ratios`, { timeoutMs: 15000 }),
  optionsContracts: (symbol) => request(`/api/options/${symbol}/contracts`, { timeoutMs: 25000 }),
  tradeGate: (payload) => request('/api/ai/trade-gate', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload),
    timeoutMs: 20000,
  }),
  backtest: (symbol, side, interval = '5m', period = '60d', score = null) => {
    const scoreQuery = Number.isFinite(score) ? `&score=${encodeURIComponent(score)}` : ''
    return request(`/api/backtest/${symbol}?side=${encodeURIComponent(side)}&interval=${encodeURIComponent(interval)}&period=${encodeURIComponent(period)}${scoreQuery}`, { timeoutMs: 25000 })
  },
  etradeStatus: () => request('/api/auth/etrade/status', { timeoutMs: 5000 }),
  etradeConnect: () => request('/api/auth/etrade/connect'),
  etradeVerify: (oauth_verifier) => request('/api/auth/etrade/verify', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ oauth_verifier }),
  }),
  etradeDisconnect: () => request('/api/auth/etrade/disconnect', { method: 'POST' }),
  etradeOptionPositions: (refresh = false) => request(`/api/admin/etrade/positions${refresh ? '?refresh=true' : ''}`, { timeoutMs: 60000 }),
  etradeAccounts: (refresh = false) => request(`/api/etrade/accounts${refresh ? '?refresh=true' : ''}`, { timeoutMs: 60000 }),
  etradeOrders: () => request('/api/etrade/orders', { timeoutMs: 15000 }),
  etradeTrades: () => request('/api/etrade/trades', { timeoutMs: 15000 }),
  optionEstimates: (symbol = '', limit = 100) => {
    const query = new URLSearchParams()
    if (symbol) query.set('symbol', symbol)
    query.set('limit', String(limit))
    return request(`/api/options/estimates?${query.toString()}`, { timeoutMs: 15000 })
  },
  optionEstimateStatus: () => request('/api/options/estimates/status', { timeoutMs: 10000 }),
  paperPortfolio: () => request('/api/paper/portfolio', { timeoutMs: 20000 }),
  paperMorningBrief: () => request('/api/paper/morning-brief', { timeoutMs: 30000 }),
  paperMorningRefresh: () => request('/api/paper/morning-brief/refresh', { method: 'POST', timeoutMs: 30000 }),
  paperMorningOutcome: (candidateId, payload) => request('/api/paper/morning-candidates/' + encodeURIComponent(candidateId) + '/outcome', {
    method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(payload), timeoutMs: 15000,
  }),
  paperPositions: () => request('/api/paper/positions', { timeoutMs: 20000 }),
  paperOrders: () => request('/api/paper/orders', { timeoutMs: 20000 }),
  paperRecommendations: (limit = 100) => request(`/api/paper/recommendations?limit=${encodeURIComponent(limit)}`, { timeoutMs: 15000 }),
  paperPerformance: () => request('/api/paper/performance', { timeoutMs: 15000 }),
  paperOrder: (payload) => request('/api/paper/orders', {
    method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(payload), timeoutMs: 15000,
  }),
  tradeReviewOverview: (filters = {}) => {
    const params = new URLSearchParams()
    Object.entries(filters || {}).forEach(([key, value]) => {
      if (value === undefined || value === null || value === '') return
      if (Array.isArray(value)) {
        if (!value.length) return
        params.set(key, value.join(','))
        return
      }
      params.set(key, value)
    })
    const query = params.toString()
    return request(`/api/trade-review/overview${query ? `?${query}` : ''}`, { timeoutMs: 30000 })
  },
  tradeReviewAccounts: (refresh = false) => request(`/api/trade-review/accounts${refresh ? '?refresh=true' : ''}`, { timeoutMs: 20000 }),
  tradeReviewRefreshAccounts: () => request('/api/trade-review/accounts/refresh', { method: 'POST', timeoutMs: 30000 }),
  tradeReviewSetSelection: (payload) => request('/api/trade-review/accounts/selection', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload),
    timeoutMs: 20000,
  }),
  tradeReviewStatus: () => request('/api/trade-review/status', { timeoutMs: 20000 }),
  tradeReviewStartSync: (payload = {}) => request('/api/trade-review/sync', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload),
    timeoutMs: 30000,
  }),
  tradeReviewCancel: () => request('/api/trade-review/cancel', { method: 'POST', timeoutMs: 20000 }),
  tradeReviewTradeDetail: (tradeId, refreshContext = false, includeAnalysis = true) =>
    request(`/api/trade-review/trades/${encodeURIComponent(tradeId)}?refresh_context=${refreshContext ? 'true' : 'false'}&include_analysis=${includeAnalysis ? 'true' : 'false'}`, { timeoutMs: 30000 }),
  tradeReviewTradeAnalysis: (tradeId) => request(`/api/trade-review/trades/${encodeURIComponent(tradeId)}/analysis`, { timeoutMs: 30000 }),
  tradeReviewUpdateTrade: (tradeId, payload) => request(`/api/trade-review/trades/${encodeURIComponent(tradeId)}`, {
    method: 'PATCH',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload),
    timeoutMs: 20000,
  }),
  alerts: () => request('/api/alerts'),
}
