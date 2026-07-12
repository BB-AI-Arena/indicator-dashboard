import { useEffect, useState } from 'react'
import { api } from '../api'

export default function Settings({ currentUser }) {
  const [config, setConfig] = useState(null)
  const [etrade, setEtrade] = useState(null)
  const [users, setUsers] = useState([])
  const [blockedIps, setBlockedIps] = useState([])
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)
  const [verifier, setVerifier] = useState('')
  const [connectMessage, setConnectMessage] = useState('')
  const [newUsername, setNewUsername] = useState('')
  const [newPassword, setNewPassword] = useState('')
  const [newRole, setNewRole] = useState('user')
  const [advisorySettings, setAdvisorySettings] = useState(null)
  const [advisoryForm, setAdvisoryForm] = useState({})
  const [socialSettings, setSocialSettings] = useState(null)
  const [socialForm, setSocialForm] = useState({})
  const [socialSourcesText, setSocialSourcesText] = useState('[]')

  const load = async () => {
    try {
      const [cfg, status] = await Promise.all([api.config(), api.etradeStatus()])
      setConfig(cfg)
      setEtrade(status)
      if (currentUser?.role === 'admin') {
        const [userRes, blockedRes, advisoryRes, socialRes] = await Promise.all([
          api.authUsers(),
          api.authBlockedIps().catch(() => ({ blocked_ips: [] })),
          api.advisorySettings().catch(() => null),
          api.socialSettings().catch(() => null),
        ])
        setUsers(userRes.users || [])
        setBlockedIps(blockedRes.blocked_ips || [])
        if (advisoryRes) {
          setAdvisorySettings(advisoryRes)
          setAdvisoryForm(advisoryRes)
        }
        if (socialRes) {
          setSocialSettings(socialRes)
          setSocialForm(socialRes)
          setSocialSourcesText(JSON.stringify(socialRes.sources || [], null, 2))
        }
      }
    } catch (e) {
      setError(e.message)
    }
  }

  useEffect(() => { load() }, [currentUser?.username])

  const connect = async () => {
    setBusy(true)
    setError('')
    setConnectMessage('')
    try {
      const res = await api.etradeConnect()
      if (res?.message) {
        setConnectMessage(res.message)
      }
      if (res?.url) {
        window.open(res.url, '_blank', 'noopener,noreferrer')
      }
      await load()
    } catch (e) {
      setError(e.message)
    } finally {
      setBusy(false)
    }
  }

  const verify = async () => {
    setBusy(true)
    setError('')
    try {
      await api.etradeVerify(verifier.trim())
      setVerifier('')
      await load()
    } catch (e) {
      setError(e.message)
    } finally {
      setBusy(false)
    }
  }

  const disconnect = async () => {
    setBusy(true)
    setError('')
    try {
      await api.etradeDisconnect()
      await load()
    } catch (e) {
      setError(e.message)
    } finally {
      setBusy(false)
    }
  }

  const createUser = async () => {
    setBusy(true)
    setError('')
    try {
      await api.authCreateUser({
        username: newUsername,
        password: newPassword,
        role: newRole,
      })
      setNewUsername('')
      setNewPassword('')
      setNewRole('user')
      await load()
    } catch (e) {
      setError(e.message)
    } finally {
      setBusy(false)
    }
  }

  const unblockIp = async (ip) => {
    setBusy(true)
    setError('')
    try {
      await api.authUnblockIp(ip)
      await load()
    } catch (e) {
      setError(e.message)
    } finally {
      setBusy(false)
    }
  }

  const saveAdvisorySettings = async () => {
    setBusy(true)
    setError('')
    try {
      const payload = {
        enabled: Boolean(advisoryForm.enabled),
        deterministic_only: Boolean(advisoryForm.deterministic_only),
        model: advisoryForm.model || 'gpt-5.6',
        fallback_model: advisoryForm.fallback_model || 'gpt-5.4-mini',
        reasoning_effort: advisoryForm.reasoning_effort || 'high',
        advisory_mode: advisoryForm.advisory_mode || 'standard',
        max_output_tokens: Number(advisoryForm.max_output_tokens || 1400),
        timeout_seconds: Number(advisoryForm.timeout_seconds || 45),
        maximum_calls_per_hour: Number(advisoryForm.maximum_calls_per_hour || 20),
        cache_duration_seconds: Number(advisoryForm.cache_duration_seconds || 1800),
        maximum_advisory_cost: Number(advisoryForm.maximum_advisory_cost || 5),
        prompt_version: advisoryForm.prompt_version || 'trade-advisory-v1',
        response_schema_version: advisoryForm.response_schema_version || 'advisory-response-v1',
      }
      const saved = await api.updateAdvisorySettings(payload)
      setAdvisorySettings(saved)
      setAdvisoryForm(saved)
    } catch (e) {
      setError(e.message)
    } finally {
      setBusy(false)
    }
  }

  const saveSocialSettings = async () => {
    setBusy(true)
    setError('')
    try {
      let sources
      try {
        sources = JSON.parse(socialSourcesText || '[]')
      } catch {
        throw new Error('Social sources must be valid JSON.')
      }
      if (!Array.isArray(sources)) throw new Error('Social sources must be a JSON array.')
      const saved = await api.updateSocialSettings({
        enabled: Boolean(socialForm.enabled),
        lookback_days: Number(socialForm.lookback_days || 7),
        baseline_days: Number(socialForm.baseline_days || 30),
        source_cache_ttl_seconds: Number(socialForm.source_cache_ttl_seconds || 900),
        minimum_mentions: Number(socialForm.minimum_mentions || 5),
        minimum_unique_authors: Number(socialForm.minimum_unique_authors || 3),
        spam_threshold: Number(socialForm.spam_threshold || 0.45),
        relevance_threshold: Number(socialForm.relevance_threshold || 0.7),
        max_items_per_source: Number(socialForm.max_items_per_source || 200),
        aliases: socialForm.aliases || {},
        sources,
      })
      setSocialSettings(saved)
      setSocialForm(saved)
      setSocialSourcesText(JSON.stringify(saved.sources || [], null, 2))
    } catch (e) {
      setError(e.message)
    } finally {
      setBusy(false)
    }
  }

  const dataCfg = config?.data || {}

  return (
    <div className="space-y-4">
      <div className="card p-4">
        <h2 className="mb-3 text-xl font-semibold">Access</h2>
        <div className="mb-3 grid gap-2 text-sm sm:grid-cols-2">
          <div>Signed in as: <span className="font-semibold">{currentUser?.username || '-'}</span></div>
          <div>Role: <span className="font-semibold">{currentUser?.role || '-'}</span></div>
        </div>
        {currentUser?.role === 'admin' && (
          <>
            <div className="mb-4 grid gap-2 rounded border border-slate-700 bg-panel2 p-3">
              <h3 className="font-semibold text-slate-100">Create user</h3>
              <div className="grid gap-2 md:grid-cols-3">
                <input className="rounded border border-slate-700 bg-slate-950 px-3 py-2 text-sm" placeholder="Username" value={newUsername} onChange={(e) => setNewUsername(e.target.value)} />
                <input className="rounded border border-slate-700 bg-slate-950 px-3 py-2 text-sm" placeholder="Password" type="password" value={newPassword} onChange={(e) => setNewPassword(e.target.value)} />
                <select className="rounded border border-slate-700 bg-slate-950 px-3 py-2 text-sm" value={newRole} onChange={(e) => setNewRole(e.target.value)}>
                  <option value="user">user</option>
                  <option value="admin">admin</option>
                </select>
              </div>
              <button disabled={busy || !newUsername.trim() || !newPassword.trim()} className="w-fit rounded bg-accent px-3 py-2 text-sm font-semibold text-slate-900 disabled:opacity-50" onClick={createUser}>Add user</button>
            </div>
            <div className="mb-4">
              <h3 className="mb-2 font-semibold text-slate-100">Users</h3>
              <div className="grid gap-2 text-sm">
                {users.map((u) => (
                  <div key={u.username} className="flex flex-wrap items-center justify-between gap-2 rounded border border-slate-700 bg-panel2 px-3 py-2">
                    <div>
                      <span className="font-semibold">{u.username}</span>
                      <span className="ml-2 text-slate-400">{u.role}</span>
                      <span className={`ml-2 ${u.active ? 'text-emerald-300' : 'text-red-300'}`}>{u.active ? 'active' : 'inactive'}</span>
                      {u.must_change_password && <span className="ml-2 rounded border border-amber-700 bg-amber-950/40 px-2 py-0.5 text-[11px] font-semibold text-amber-200">password change required</span>}
                    </div>
                    <div className="text-xs text-slate-400">Last login: {u.last_login_at || '-'}</div>
                  </div>
                ))}
                {!users.length && <div className="text-sm text-slate-400">No users yet.</div>}
              </div>
            </div>
            <div className="mb-4">
              <h3 className="mb-2 font-semibold text-slate-100">Blocked IPs</h3>
              <div className="grid gap-2 text-sm">
                {blockedIps.map((row) => (
                  <div key={row.ip_address} className="flex flex-wrap items-center justify-between gap-2 rounded border border-red-900/50 bg-red-950/20 px-3 py-2">
                    <div>
                      <span className="font-semibold">{row.ip_address}</span>
                      <span className="ml-2 text-red-200">{row.reason}</span>
                    </div>
                    <button disabled={busy} className="rounded bg-red-700 px-2 py-1 text-xs font-semibold text-white disabled:opacity-50" onClick={() => unblockIp(row.ip_address)}>Unblock</button>
                  </div>
                ))}
                {!blockedIps.length && <div className="text-sm text-slate-400">No blocked IPs.</div>}
              </div>
            </div>
          </>
        )}
      </div>

      <div className="card p-4">
        <h2 className="mb-3 text-xl font-semibold">E*TRADE</h2>
        {etrade && (
          <div className="mb-3 grid gap-2 text-sm sm:grid-cols-2">
            <div>Configured: <span className="font-semibold">{etrade.configured ? 'Yes' : 'No'}</span></div>
            <div>Connected: <span className="font-semibold">{etrade.connected ? 'Yes' : 'No'}</span></div>
            <div>Mode: <span className="font-semibold">{etrade.sandbox ? 'Sandbox' : 'Live'}</span></div>
            <div>Message: <span className="font-semibold">{etrade.message}</span></div>
          </div>
        )}
        <div className="flex gap-2">
          <button disabled={busy} className="rounded bg-accent px-3 py-2 text-sm font-semibold text-slate-900 disabled:opacity-50" onClick={connect}>Connect E*TRADE</button>
          <button disabled={busy} className="rounded bg-slate-700 px-3 py-2 text-sm font-semibold disabled:opacity-50" onClick={disconnect}>Disconnect E*TRADE</button>
        </div>
        {connectMessage && <p className="mt-2 text-xs text-amber-300">{connectMessage}</p>}
        <div className="mt-3 flex flex-col gap-2 sm:flex-row">
          <input
            className="w-full rounded border border-slate-700 bg-panel2 px-3 py-2 text-sm"
            placeholder="Paste E*TRADE verifier code (for OOB flow)"
            value={verifier}
            onChange={(e) => setVerifier(e.target.value)}
          />
          <button disabled={busy || !verifier.trim()} className="rounded bg-emerald-600 px-3 py-2 text-sm font-semibold text-slate-900 disabled:opacity-50" onClick={verify}>Submit Verifier</button>
        </div>
      </div>

      <div className="card p-4">
        <h2 className="mb-3 text-xl font-semibold">Providers</h2>
        <div className="grid gap-2 text-sm sm:grid-cols-2 lg:grid-cols-4">
          <div>Quotes: <span className="font-semibold">{dataCfg.quotes_provider || '-'}</span></div>
          <div>Options: <span className="font-semibold">{dataCfg.options_provider || '-'}</span></div>
          <div>Candles: <span className="font-semibold">{dataCfg.candles_provider || '-'}</span></div>
          <div>Historical: <span className="font-semibold">{dataCfg.historical_candles_provider || '-'}</span></div>
          <div>Quotes Fallback: <span className="font-semibold">{dataCfg.quotes_provider_fallback || '-'}</span></div>
          <div>Candles Fallback: <span className="font-semibold">{dataCfg.candles_provider_fallback || '-'}</span></div>
          <div>Historical Fallback: <span className="font-semibold">{dataCfg.historical_candles_provider_fallback || '-'}</span></div>
          <div>Default Fallback: <span className="font-semibold">{dataCfg.fallback_provider || '-'}</span></div>
        </div>
      </div>

      {config?.paper_portfolio && (
        <div className="card p-4">
          <h2 className="mb-3 text-xl font-semibold">Paper Options Risk Rules</h2>
          <div className="grid gap-2 text-sm sm:grid-cols-2 lg:grid-cols-4">
            <div>Max deployment: <span className="font-semibold">{config.paper_portfolio.max_deployment_pct}%</span></div>
            <div>Reserve: <span className="font-semibold">{config.paper_portfolio.reserve_pct}%</span></div>
            <div>Trailing mode: <span className="font-semibold">{config.paper_portfolio.trailing_mode}</span></div>
            <div>Stop execution: <span className="font-semibold">{config.paper_portfolio.stop_execution_mode}</span></div>
            <div>Trail activation: <span className="font-semibold">{config.paper_portfolio.profit_trail_activation_pct}%</span></div>
            <div>Trail distance: <span className="font-semibold">{config.paper_portfolio.profit_trail_pct}%</span></div>
            <div>Loser review: <span className="font-semibold">{config.paper_portfolio.mandatory_loser_review_et} ET</span></div>
            <div>Liquidation cutoff: <span className="font-semibold">{config.paper_portfolio.mandatory_loser_cutoff_et} ET</span></div>
          </div>
          <p className="mt-3 text-xs text-slate-400">Edit these paper-only rules in <code>config/config.yml</code>. The simulator never treats the 75% deployment cap as an acceptable-loss limit.</p>
        </div>
      )}

      {currentUser?.role === 'admin' && (
        <div className="card p-4">
          <h2 className="mb-3 text-xl font-semibold">GPT Advisory Controls</h2>
          <p className="mb-3 text-sm text-slate-400">
            Flagship model advice is cached and cannot override deterministic hard gates. Simple labels and bulk analysis should stay deterministic or use cheaper models.
          </p>
          {!advisorySettings && <p className="text-sm text-slate-400">Loading advisory settings...</p>}
          {advisorySettings && (
            <div className="space-y-3">
              <div className="grid gap-3 md:grid-cols-2 lg:grid-cols-4">
                <label className="grid gap-1 text-sm">
                  <span className="text-slate-400">Flagship model</span>
                  <input className="rounded border border-slate-700 bg-slate-950 px-3 py-2" value={advisoryForm.model || ''} onChange={(e) => setAdvisoryForm((p) => ({ ...p, model: e.target.value }))} />
                </label>
                <label className="grid gap-1 text-sm">
                  <span className="text-slate-400">Fallback model</span>
                  <input className="rounded border border-slate-700 bg-slate-950 px-3 py-2" value={advisoryForm.fallback_model || ''} onChange={(e) => setAdvisoryForm((p) => ({ ...p, fallback_model: e.target.value }))} />
                </label>
                <label className="grid gap-1 text-sm">
                  <span className="text-slate-400">Reasoning effort</span>
                  <select className="rounded border border-slate-700 bg-slate-950 px-3 py-2" value={advisoryForm.reasoning_effort || 'high'} onChange={(e) => setAdvisoryForm((p) => ({ ...p, reasoning_effort: e.target.value }))}>
                    <option value="low">low</option>
                    <option value="medium">medium</option>
                    <option value="high">high</option>
                  </select>
                </label>
                <label className="grid gap-1 text-sm">
                  <span className="text-slate-400">Advisory mode</span>
                  <input className="rounded border border-slate-700 bg-slate-950 px-3 py-2" value={advisoryForm.advisory_mode || 'standard'} onChange={(e) => setAdvisoryForm((p) => ({ ...p, advisory_mode: e.target.value }))} />
                </label>
                <label className="grid gap-1 text-sm">
                  <span className="text-slate-400">Max output tokens</span>
                  <input type="number" className="rounded border border-slate-700 bg-slate-950 px-3 py-2" value={advisoryForm.max_output_tokens || 1400} onChange={(e) => setAdvisoryForm((p) => ({ ...p, max_output_tokens: e.target.value }))} />
                </label>
                <label className="grid gap-1 text-sm">
                  <span className="text-slate-400">Timeout seconds</span>
                  <input type="number" className="rounded border border-slate-700 bg-slate-950 px-3 py-2" value={advisoryForm.timeout_seconds || 45} onChange={(e) => setAdvisoryForm((p) => ({ ...p, timeout_seconds: e.target.value }))} />
                </label>
                <label className="grid gap-1 text-sm">
                  <span className="text-slate-400">Max calls/hour</span>
                  <input type="number" className="rounded border border-slate-700 bg-slate-950 px-3 py-2" value={advisoryForm.maximum_calls_per_hour || 20} onChange={(e) => setAdvisoryForm((p) => ({ ...p, maximum_calls_per_hour: e.target.value }))} />
                </label>
                <label className="grid gap-1 text-sm">
                  <span className="text-slate-400">Cache seconds</span>
                  <input type="number" className="rounded border border-slate-700 bg-slate-950 px-3 py-2" value={advisoryForm.cache_duration_seconds || 1800} onChange={(e) => setAdvisoryForm((p) => ({ ...p, cache_duration_seconds: e.target.value }))} />
                </label>
                <label className="grid gap-1 text-sm">
                  <span className="text-slate-400">Max advisory cost</span>
                  <input type="number" step="0.01" className="rounded border border-slate-700 bg-slate-950 px-3 py-2" value={advisoryForm.maximum_advisory_cost || 5} onChange={(e) => setAdvisoryForm((p) => ({ ...p, maximum_advisory_cost: e.target.value }))} />
                </label>
                <label className="grid gap-1 text-sm">
                  <span className="text-slate-400">Prompt version</span>
                  <input className="rounded border border-slate-700 bg-slate-950 px-3 py-2" value={advisoryForm.prompt_version || ''} onChange={(e) => setAdvisoryForm((p) => ({ ...p, prompt_version: e.target.value }))} />
                </label>
                <label className="grid gap-1 text-sm">
                  <span className="text-slate-400">Schema version</span>
                  <input className="rounded border border-slate-700 bg-slate-950 px-3 py-2" value={advisoryForm.response_schema_version || ''} onChange={(e) => setAdvisoryForm((p) => ({ ...p, response_schema_version: e.target.value }))} />
                </label>
                <label className="flex items-center gap-2 rounded border border-slate-700 bg-panel2 px-3 py-2 text-sm">
                  <input type="checkbox" checked={Boolean(advisoryForm.enabled)} onChange={(e) => setAdvisoryForm((p) => ({ ...p, enabled: e.target.checked }))} />
                  <span>Enable GPT advisory</span>
                </label>
                <label className="flex items-center gap-2 rounded border border-slate-700 bg-panel2 px-3 py-2 text-sm">
                  <input type="checkbox" checked={Boolean(advisoryForm.deterministic_only)} onChange={(e) => setAdvisoryForm((p) => ({ ...p, deterministic_only: e.target.checked }))} />
                  <span>Deterministic-only mode</span>
                </label>
              </div>
              <button disabled={busy} className="rounded bg-accent px-3 py-2 text-sm font-semibold text-slate-900 disabled:opacity-50" onClick={saveAdvisorySettings}>
                Save Advisory Controls
              </button>
            </div>
          )}
        </div>
      )}

      {currentUser?.role === 'admin' && (
        <div className="card p-4">
          <h2 className="mb-3 text-xl font-semibold">Social Intelligence Controls</h2>
          <p className="mb-3 text-sm text-slate-400">
            Social data is supporting evidence only. Configure authorized RSS or authenticated JSON API sources; use <code>token_env</code> to reference credentials without entering them here.
          </p>
          {socialSettings && (
            <div className="space-y-3">
              <div className="grid gap-3 md:grid-cols-2 lg:grid-cols-4">
                <label className="flex items-center gap-2 rounded border border-slate-700 bg-panel2 px-3 py-2 text-sm">
                  <input type="checkbox" checked={Boolean(socialForm.enabled)} onChange={(e) => setSocialForm((p) => ({ ...p, enabled: e.target.checked }))} />
                  <span>Enable social intelligence</span>
                </label>
                {[
                  ['lookback_days', 'Lookback days', 7],
                  ['baseline_days', 'Baseline days', 30],
                  ['source_cache_ttl_seconds', 'Cache seconds', 900],
                  ['minimum_mentions', 'Minimum mentions', 5],
                  ['minimum_unique_authors', 'Minimum authors', 3],
                  ['spam_threshold', 'Spam threshold', 0.45],
                  ['relevance_threshold', 'Relevance threshold', 0.7],
                  ['max_items_per_source', 'Max items/source', 200],
                ].map(([key, label, fallback]) => (
                  <label key={key} className="grid gap-1 text-sm">
                    <span className="text-slate-400">{label}</span>
                    <input type="number" step={Number(fallback) < 1 ? '0.05' : '1'} className="rounded border border-slate-700 bg-slate-950 px-3 py-2" value={socialForm[key] ?? fallback} onChange={(e) => setSocialForm((p) => ({ ...p, [key]: e.target.value }))} />
                  </label>
                ))}
              </div>
              <label className="grid gap-1 text-sm">
                <span className="text-slate-400">Authorized sources JSON</span>
                <textarea className="min-h-40 rounded border border-slate-700 bg-slate-950 px-3 py-2 font-mono text-xs" value={socialSourcesText} onChange={(e) => setSocialSourcesText(e.target.value)} />
              </label>
              <button disabled={busy} className="rounded bg-accent px-3 py-2 text-sm font-semibold text-slate-900 disabled:opacity-50" onClick={saveSocialSettings}>Save Social Controls</button>
            </div>
          )}
        </div>
      )}

      <div className="card p-4">
        <h2 className="mb-3 text-xl font-semibold">Settings (Raw Config)</h2>
        {error && <p className="text-bear">{error}</p>}
        {!config && !error && <p className="text-slate-400">Loading...</p>}
        {config && <pre className="overflow-auto rounded bg-panel2 p-3 text-xs">{JSON.stringify(config, null, 2)}</pre>}
        <p className="mt-3 text-sm text-slate-400">Edit config at <code>config/config.yml</code>, then restart backend.</p>
      </div>
    </div>
  )
}
