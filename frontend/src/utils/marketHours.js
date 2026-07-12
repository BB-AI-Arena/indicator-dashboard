const EASTERN_TIME_ZONE = 'America/New_York'

function easternParts(now = new Date()) {
  const parts = new Intl.DateTimeFormat('en-US', {
    timeZone: EASTERN_TIME_ZONE,
    year: 'numeric',
    month: '2-digit',
    day: '2-digit',
    weekday: 'short',
    hour: '2-digit',
    minute: '2-digit',
    hourCycle: 'h23',
  }).formatToParts(now)

  const out = {}
  parts.forEach((part) => {
    if (part.type !== 'literal') out[part.type] = part.value
  })
  return {
    year: Number(out.year),
    month: Number(out.month),
    day: Number(out.day),
    weekday: out.weekday,
    hour: Number(out.hour),
    minute: Number(out.minute),
    dateKey: `${out.year}-${out.month}-${out.day}`,
    displayTime: `${out.hour}:${out.minute} ET`,
  }
}

function dateKey(year, month, day) {
  return `${year}-${String(month).padStart(2, '0')}-${String(day).padStart(2, '0')}`
}

function utcWeekday(year, month, day) {
  return new Date(Date.UTC(year, month - 1, day)).getUTCDay()
}

function nthWeekday(year, month, weekday, nth) {
  let count = 0
  for (let day = 1; day <= 31; day += 1) {
    const d = new Date(Date.UTC(year, month - 1, day))
    if (d.getUTCMonth() !== month - 1) break
    if (d.getUTCDay() === weekday) {
      count += 1
      if (count === nth) return dateKey(year, month, day)
    }
  }
  return null
}

function lastWeekday(year, month, weekday) {
  for (let day = 31; day >= 1; day -= 1) {
    const d = new Date(Date.UTC(year, month - 1, day))
    if (d.getUTCMonth() !== month - 1) continue
    if (d.getUTCDay() === weekday) return dateKey(year, month, day)
  }
  return null
}

function observedFixedHoliday(year, month, day) {
  const weekday = utcWeekday(year, month, day)
  if (weekday === 6) return dateKey(year, month, day - 1)
  if (weekday === 0) return dateKey(year, month, day + 1)
  return dateKey(year, month, day)
}

function easterDate(year) {
  const a = year % 19
  const b = Math.floor(year / 100)
  const c = year % 100
  const d = Math.floor(b / 4)
  const e = b % 4
  const f = Math.floor((b + 8) / 25)
  const g = Math.floor((b - f + 1) / 3)
  const h = (19 * a + b - d - g + 15) % 30
  const i = Math.floor(c / 4)
  const k = c % 4
  const l = (32 + 2 * e + 2 * i - h - k) % 7
  const m = Math.floor((a + 11 * h + 22 * l) / 451)
  const month = Math.floor((h + l - 7 * m + 114) / 31)
  const day = ((h + l - 7 * m + 114) % 31) + 1
  return new Date(Date.UTC(year, month - 1, day))
}

function goodFriday(year) {
  const easter = easterDate(year)
  easter.setUTCDate(easter.getUTCDate() - 2)
  return dateKey(year, easter.getUTCMonth() + 1, easter.getUTCDate())
}

function marketHolidays(year) {
  return new Set([
    observedFixedHoliday(year, 1, 1),
    nthWeekday(year, 1, 1, 3),
    nthWeekday(year, 2, 1, 3),
    goodFriday(year),
    lastWeekday(year, 5, 1),
    observedFixedHoliday(year, 6, 19),
    observedFixedHoliday(year, 7, 4),
    nthWeekday(year, 9, 1, 1),
    nthWeekday(year, 11, 4, 4),
    observedFixedHoliday(year, 12, 25),
  ].filter(Boolean))
}

function earlyCloseKey(year, month, day) {
  const weekday = utcWeekday(year, month, day)
  if (weekday === 0 || weekday === 6) return null
  return dateKey(year, month, day)
}

function marketEarlyCloses(year) {
  const thanksgiving = nthWeekday(year, 11, 4, 4)
  const thanksgivingDate = thanksgiving ? Number(thanksgiving.slice(-2)) : null
  return new Set([
    thanksgivingDate ? earlyCloseKey(year, 11, thanksgivingDate + 1) : null,
    earlyCloseKey(year, 12, 24),
    earlyCloseKey(year, 7, 3),
  ].filter(Boolean))
}

export function getMarketStatus(now = new Date()) {
  const eastern = easternParts(now)
  const weekdayIndex = ['Sun', 'Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat'].indexOf(eastern.weekday)
  const holidays = new Set([
    ...marketHolidays(eastern.year - 1),
    ...marketHolidays(eastern.year),
    ...marketHolidays(eastern.year + 1),
  ])
  const earlyCloses = marketEarlyCloses(eastern.year)
  const minuteOfDay = eastern.hour * 60 + eastern.minute
  const openMinute = 9 * 60 + 30
  const closeMinute = earlyCloses.has(eastern.dateKey) ? 13 * 60 : 16 * 60

  let isOpen = true
  let reason = 'Regular market hours'
  if (weekdayIndex === 0 || weekdayIndex === 6) {
    isOpen = false
    reason = 'Weekend'
  } else if (holidays.has(eastern.dateKey)) {
    isOpen = false
    reason = 'US market holiday'
  } else if (minuteOfDay < openMinute) {
    isOpen = false
    reason = 'Before regular session'
  } else if (minuteOfDay >= closeMinute) {
    isOpen = false
    reason = earlyCloses.has(eastern.dateKey) ? 'After early close' : 'After regular session'
  }

  return {
    isOpen,
    label: isOpen ? 'MARKET OPEN' : 'MARKET CLOSED',
    reason,
    easternTime: eastern.displayTime,
    dateKey: eastern.dateKey,
    closeTime: earlyCloses.has(eastern.dateKey) ? '1:00 PM ET' : '4:00 PM ET',
  }
}
