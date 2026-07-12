import { useState } from 'react'
import { api } from '../api'

export default function LoginPage({ setupRequired, blockedReason, mustChangePassword = false, currentUser = null, onAuthenticated, onPasswordChanged }) {
  const [loginUser, setLoginUser] = useState('')
  const [loginPass, setLoginPass] = useState('')
  const [authToken, setAuthToken] = useState(() => api.getAuthToken())
  const [setupKey, setSetupKey] = useState('')
  const [setupUser, setSetupUser] = useState('')
  const [setupPass, setSetupPass] = useState('')
  const [setupConfirm, setSetupConfirm] = useState('')
  const [newPass, setNewPass] = useState('')
  const [newPassConfirm, setNewPassConfirm] = useState('')
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)

  const finishAuth = (session) => {
    if (session?.access_token) {
      api.setAuthToken(session.access_token)
      setAuthToken(session.access_token)
    }
    onAuthenticated(session || null)
  }

  const login = async (event) => {
    event.preventDefault()
    setBusy(true)
    setError('')
    try {
      const session = await api.authLogin({ username: loginUser, password: loginPass })
      finishAuth(session)
    } catch (e) {
      setError(e.message)
    } finally {
      setBusy(false)
    }
  }

  const bootstrap = async (event) => {
    event.preventDefault()
    setBusy(true)
    setError('')
    try {
      if (setupPass !== setupConfirm) {
        throw new Error('Passwords do not match')
      }
      const session = await api.authBootstrap({
        setup_key: setupKey,
        username: setupUser,
        password: setupPass,
        role: 'admin',
      })
      finishAuth(session?.session || session)
    } catch (e) {
      setError(e.message)
    } finally {
      setBusy(false)
    }
  }

  const changePassword = async (event) => {
    event.preventDefault()
    setBusy(true)
    setError('')
    try {
      if (newPass !== newPassConfirm) {
        throw new Error('Passwords do not match')
      }
      const res = await api.authChangePassword({ new_password: newPass }, authToken)
      setNewPass('')
      setNewPassConfirm('')
      onPasswordChanged(res?.user || currentUser)
    } catch (e) {
      setError(e.message)
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="mx-auto flex min-h-screen max-w-7xl items-center justify-center p-4 md:p-6">
      <div className="w-full max-w-2xl space-y-4">
        <div className="card p-6">
          <h1 className="text-2xl font-bold tracking-wide">Indicator Command Center</h1>
          <p className="mt-1 text-sm text-slate-400">Texas access only. Sign in to continue.</p>
          {blockedReason && <div className="mt-4 rounded border border-red-800/60 bg-red-950/40 p-3 text-sm text-red-200">{blockedReason}</div>}
        </div>

        {mustChangePassword ? (
          <form className="card space-y-3 p-6" onSubmit={changePassword}>
            <h2 className="text-lg font-semibold text-slate-100">Change your password</h2>
            <p className="text-sm text-slate-400">Account: <span className="font-semibold text-slate-200">{currentUser?.username || '-'}</span>. Access is locked until the initial password is changed.</p>
            {error && <div className="rounded border border-red-800/60 bg-red-950/40 p-3 text-sm text-red-200">{error}</div>}
            <input className="w-full rounded border border-slate-700 bg-panel2 px-3 py-2 text-sm" placeholder="New password" type="password" value={newPass} onChange={(e) => setNewPass(e.target.value)} />
            <input className="w-full rounded border border-slate-700 bg-panel2 px-3 py-2 text-sm" placeholder="Confirm new password" type="password" value={newPassConfirm} onChange={(e) => setNewPassConfirm(e.target.value)} />
            <button disabled={busy} className="rounded bg-accent px-4 py-2 text-sm font-semibold text-slate-900 disabled:opacity-50" type="submit">Save new password</button>
          </form>
        ) : setupRequired ? (
          <form className="card space-y-3 p-6" onSubmit={bootstrap}>
            <h2 className="text-lg font-semibold text-slate-100">Create first admin user</h2>
            <p className="text-sm text-slate-400">Use the bootstrap key from the backend environment to initialize access.</p>
            {error && <div className="rounded border border-red-800/60 bg-red-950/40 p-3 text-sm text-red-200">{error}</div>}
            <input className="w-full rounded border border-slate-700 bg-panel2 px-3 py-2 text-sm" placeholder="Bootstrap key (optional if unset)" value={setupKey} onChange={(e) => setSetupKey(e.target.value)} />
            <input className="w-full rounded border border-slate-700 bg-panel2 px-3 py-2 text-sm" placeholder="Admin username" value={setupUser} onChange={(e) => setSetupUser(e.target.value)} />
            <input className="w-full rounded border border-slate-700 bg-panel2 px-3 py-2 text-sm" placeholder="Admin password" type="password" value={setupPass} onChange={(e) => setSetupPass(e.target.value)} />
            <input className="w-full rounded border border-slate-700 bg-panel2 px-3 py-2 text-sm" placeholder="Confirm password" type="password" value={setupConfirm} onChange={(e) => setSetupConfirm(e.target.value)} />
            <button disabled={busy} className="rounded bg-accent px-4 py-2 text-sm font-semibold text-slate-900 disabled:opacity-50" type="submit">Create admin and sign in</button>
          </form>
        ) : (
          <form className="card space-y-3 p-6" onSubmit={login}>
            <h2 className="text-lg font-semibold text-slate-100">Sign in</h2>
            {error && <div className="rounded border border-red-800/60 bg-red-950/40 p-3 text-sm text-red-200">{error}</div>}
            <input
              autoComplete="username"
              autoCapitalize="none"
              spellCheck={false}
              className="w-full rounded border border-slate-700 bg-panel2 px-3 py-2 text-sm"
              placeholder="Username"
              value={loginUser}
              onChange={(e) => setLoginUser(e.target.value)}
            />
            <input
              autoComplete="current-password"
              autoCapitalize="none"
              spellCheck={false}
              className="w-full rounded border border-slate-700 bg-panel2 px-3 py-2 text-sm"
              placeholder="Password"
              type="password"
              value={loginPass}
              onChange={(e) => setLoginPass(e.target.value)}
            />
            <button disabled={busy} className="rounded bg-accent px-4 py-2 text-sm font-semibold text-slate-900 disabled:opacity-50" type="submit">Login</button>
          </form>
        )}
      </div>
    </div>
  )
}
