const CENTRAL_TIME_ZONE = 'America/Chicago'
const EASTERN_TIME_ZONE = 'America/New_York'

function parseApiTimestamp(value) {
  if (!value) return null
  if (value instanceof Date) return value
  if (typeof value === 'number') {
    return new Date(value > 1_000_000_000_000 ? value : value * 1000)
  }

  const raw = String(value).trim()
  if (!raw) return null

  const hasTimezone = /(?:Z|[+-]\d{2}:?\d{2})$/i.test(raw)
  const normalized = hasTimezone ? raw : `${raw}Z`
  const parsed = new Date(normalized)
  return Number.isNaN(parsed.getTime()) ? null : parsed
}

export function formatCentralTime(value, options = {}) {
  const date = parseApiTimestamp(value)
  if (!date) return '-'

  return new Intl.DateTimeFormat('en-US', {
    timeZone: CENTRAL_TIME_ZONE,
    year: 'numeric',
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
    second: options.seconds === false ? undefined : '2-digit',
    timeZoneName: 'short',
  }).format(date)
}

export function formatEasternTime(value, options = {}) {
  const date = parseApiTimestamp(value)
  if (!date) return '-'

  return new Intl.DateTimeFormat('en-US', {
    timeZone: EASTERN_TIME_ZONE,
    year: 'numeric',
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
    second: options.seconds === false ? undefined : '2-digit',
    timeZoneName: 'short',
  }).format(date)
}

export function getCentralHour(value) {
  const date = parseApiTimestamp(value)
  if (!date) return null

  const parts = new Intl.DateTimeFormat('en-US', {
    timeZone: CENTRAL_TIME_ZONE,
    hour: '2-digit',
    hour12: false,
  }).formatToParts(date)
  const hour = Number(parts.find((part) => part.type === 'hour')?.value)
  return Number.isFinite(hour) ? hour : null
}

export function getEasternHour(value) {
  const date = parseApiTimestamp(value)
  if (!date) return null

  const parts = new Intl.DateTimeFormat('en-US', {
    timeZone: EASTERN_TIME_ZONE,
    hour: '2-digit',
    hour12: false,
  }).formatToParts(date)
  const hour = Number(parts.find((part) => part.type === 'hour')?.value)
  return Number.isFinite(hour) ? hour : null
}
