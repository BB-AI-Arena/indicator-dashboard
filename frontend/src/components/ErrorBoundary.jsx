import React from 'react'

export default class ErrorBoundary extends React.Component {
  constructor(props) {
    super(props)
    this.state = { hasError: false, message: '' }
  }

  static getDerivedStateFromError(error) {
    return { hasError: true, message: error?.message || 'Unexpected UI error' }
  }

  componentDidCatch(error, info) {
    // Keep log local to browser devtools; do not crash entire app tree.
    // eslint-disable-next-line no-console
    console.error('UI crash captured by ErrorBoundary', error, info)
  }

  reset = () => {
    this.setState({ hasError: false, message: '' })
    window.location.hash = ''
    window.location.reload()
  }

  render() {
    if (this.state.hasError) {
      return (
        <div className="min-h-screen bg-bg p-6 text-slate-100">
          <div className="mx-auto max-w-2xl rounded border border-red-800/50 bg-panel p-5">
            <h2 className="text-xl font-bold text-red-300">Dashboard crashed on this view</h2>
            <p className="mt-2 text-sm text-slate-300">{this.state.message}</p>
            <p className="mt-2 text-xs text-slate-400">Use Recover to reload the app without losing backend state.</p>
            <button className="mt-4 rounded bg-accent px-4 py-2 font-semibold text-slate-900" onClick={this.reset}>Recover</button>
          </div>
        </div>
      )
    }
    return this.props.children
  }
}
