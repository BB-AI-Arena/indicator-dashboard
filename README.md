# Indicator Dashboard

A local, read-only trading dashboard for scanning equities, reviewing technical conditions, and ranking option contract candidates by liquidity and execution quality.

This project is built for decision support. It does not place trades, route orders, manage positions, or provide financial advice.

## Core Capabilities

- **Technical scanner**: scores watchlist symbols as `LONG`, `SHORT`, or `NEUTRAL` using VWAP, EMA alignment, RSI, MACD, Bollinger midpoint, volume, and prior-bar break logic.
- **Options sentiment**: calculates put/call and call/put volume and open-interest ratios across the configured number of expirations.
- **Contract ranking**: ranks call and put candidates after filtering out expired, illiquid, missing-quote, and wide-spread contracts.
- **AI validation gate**: optionally sends a fully factual candidate payload to OpenAI and displays a recommendation only when the model returns `PROCEED`.
- **Session play plan**: displays scenario-based target/stop levels and historical setup context for directional symbols.
- **Persistent ticker intelligence profiles**: creates a durable profile for each watchlist ticker, stores reusable candle/setup/news/options statistics, and refreshes incrementally from SQLite instead of rebuilding on page load.
- **Decision dashboard**: the main dashboard renders only top long/short candidates, forming setups, next-session bias, and no-trade conditions from stored profiles and cached analysis.
- **Watchlist Intelligence**: detailed watchlist coverage, profile state, historical tables, provider diagnostics, and ticker drill-ins live outside the main decision page.
- **Model-based advisory layer**: packages deterministic market facts into a structured OpenAI advisory request, validates the model output, and falls back to deterministic analysis when hard gates fail or the model is unavailable.
- **Provider abstraction**: supports E*TRADE for quotes/options, Yahoo/yfinance, Alpha Vantage, Finnhub, Stooq, and Twelve Data for candles and quote fallback.
- **Local persistence**: stores watchlist, scans, alerts, OAuth token data, and cache files under `./data`.
- **Central Time UI**: displays timestamps in `America/Chicago` (`CST`/`CDT` depending on daylight saving time).

## What This Is Not

- Not an execution system.
- Not an automated trading bot.
- Not a broker integration for order placement.
- Not a source of guaranteed real-time data.
- Not financial, tax, or investment advice.

Use the output as a structured research layer, not as a standalone trade signal.

## Architecture

```text
indicator-dashboard/
├── backend/              FastAPI API, scanner, provider integrations, SQLite models
├── frontend/             React/Vite dashboard UI
├── config/config.yml     Scanner, indicator, provider, option-filter, and cache config
├── data/                 Local runtime state; ignored except data/.gitkeep
└── docker-compose.yml    Backend/frontend local runtime
```

### Backend

- Framework: `FastAPI`
- Database: `SQLite`
- Indicators: `pandas` + `numpy`
- Providers:
  - `etrade`: quotes, expirations, option chains, ratios, ranked contracts
  - `alphavantage`: quotes and historical candles
  - `finnhub`: quotes and historical candles
  - `stooq`: historical candles when CSV downloads are available
  - `yahoo`: candles, quotes, options fallback
  - `twelvedata`: candles

### Frontend

- Framework: `React`
- Build: `Vite`
- Charts: `lightweight-charts`, `recharts`
- Styling: `Tailwind CSS`

## Safety and Secrets

The repository is configured to avoid publishing runtime secrets and data:

- `.env` and `.env.*` are ignored.
- `data/*` is ignored except `data/.gitkeep`.
- SQLite DBs, E*TRADE token JSON, provider cache files, `node_modules`, and build output are ignored.

Keep broker credentials and OAuth tokens local.

## Access Control

The dashboard now starts at a login screen. Access is gated by IP and session token:

- The backend blocks repeated login failures from the same IP after 3 tries.
- The backend rejects requests unless the client is in Texas, United States.
- On a fresh database, set `AUTH_INITIAL_PASSWORD` locally if you want the app to seed the `admin`, `Brant`, and `Nik` accounts; the value is never stored in the repository and users are forced to change it on first login.
- First-time setup can still use `AUTH_BOOTSTRAP_TOKEN` from `.env` to create the initial admin user if you want to override the seeded flow.
- Admin users can add or disable additional users from the `Settings` tab after signing in.

If you are running locally, private-network addresses are treated as local-dev traffic. For a strict public deployment, put the app behind a proxy that forwards the real client IP and set the security policy accordingly.

## Setup

### 1. Create `.env`

```bash
cp .env.example .env
```

Required for E*TRADE:

```bash
ETRADE_CONSUMER_KEY=...
ETRADE_CONSUMER_SECRET=...
ETRADE_SANDBOX=false
ETRADE_CALLBACK_URL=http://localhost:8000/api/auth/etrade/callback
```

Optional for Twelve Data candles:

```bash
TWELVEDATA_API_KEY=...
TWELVEDATA_BASE_URL=https://api.twelvedata.com
```

Optional for Alpha Vantage quotes and historical candles:

```bash
ALPHA_VANTAGE_API_KEY=...
ALPHA_VANTAGE_OUTPUT_FORMAT=json
```

Optional for Finnhub quotes and historical candles:

```bash
FINNHUB_API_KEY=...
```

Optional for the AI validation gate:

```bash
OPENAI_API_KEY=...
OPENAI_MODEL=gpt-5.6
AUTH_BOOTSTRAP_TOKEN=...
```

Optional for the trade-advisory layer:

```bash
OPENAI_ADVISORY_MODEL=gpt-5.6
OPENAI_ADVISORY_FALLBACK_MODEL=gpt-5.4-mini
OPENAI_ADVISORY_REASONING_EFFORT=high
OPENAI_ADVISORY_MODE=standard
OPENAI_MODEL_POSITION_ADVICE=
OPENAI_MODEL_TRADE_REVIEW=
```

`OPENAI_MODEL_POSITION_ADVICE` and `OPENAI_MODEL_TRADE_REVIEW` are optional per-feature overrides. If unset, position management and hard-truth trade review use `OPENAI_ADVISORY_MODEL`.

On a fresh install, set a strong local `AUTH_INITIAL_PASSWORD` in `.env` before starting the backend if you want the three initial accounts created. The app forces a password change immediately after sign-in. No initial username/password is documented or hardcoded in the repository.

If exposing the dashboard over a LAN IP or a different backend port, update `ETRADE_CALLBACK_URL` to match the callback registered with E*TRADE.

### 2. Start the app

```bash
docker compose up -d --build
```

### 3. Open the dashboard

- Frontend: `http://localhost:5173`
- Backend API docs: `http://localhost:8000/docs`
- Health: `http://localhost:8000/api/health`
- E*TRADE status: `http://localhost:8000/api/auth/etrade/status`

If `BACKEND_PORT` is set in `.env`, replace `8000` with that port.

## E*TRADE Authentication

1. Set `ETRADE_CONSUMER_KEY`, `ETRADE_CONSUMER_SECRET`, and `ETRADE_CALLBACK_URL` in `.env`.
2. Start the app.
3. Open the dashboard `Settings` tab.
4. Click `Connect E*TRADE`.
5. Complete OAuth authorization.
6. Confirm `/api/auth/etrade/status` reports connected.

Token data is stored under `./data` and is intentionally not committed.

## Configuration

Main configuration lives in `config/config.yml`.

### Scanner

```yaml
scan:
  symbols:
    - SPY
    - QQQ
  interval: 5m
  period: 5d
  sleep_seconds: 300
  min_score_to_alert: 6
  alert_cooldown_minutes: 30
```

- `symbols`: seed watchlist.
- `interval`: candle interval used by the scanner.
- `period`: candle lookback window.
- `sleep_seconds`: scanner loop cadence.
- `min_score_to_alert`: minimum score required to create an alert.

### Indicators

```yaml
indicators:
  ema_fast: 8
  ema_slow: 21
  ema_trend: 50
  rsi_period: 14
  atr_period: 14
  bollinger_period: 20
  bollinger_std: 2
  volume_avg_period: 20
  macd_fast: 12
  macd_slow: 26
  macd_signal: 9
```

The scanner gives one point for each matching directional condition. Current max score is `8`.

### Options

```yaml
options:
  enabled: true
  expirations_to_check: 3
  min_volume: 50
  min_open_interest: 100
  max_spread_pct: 15
  recommended_max_spread_pct: 5
  max_quote_age_seconds: 300
  preferred_delta_min: 0.30
  preferred_delta_max: 0.55
```

Contract candidates are rejected if they:

- are expired relative to `America/Chicago`;
- have missing or zero bid/ask;
- exceed `max_spread_pct`;
- have volume below `min_volume`;
- have open interest below `min_open_interest`.

Remaining contracts are scored by spread quality, volume, open interest, moneyness, expiration risk, quote quality, quote staleness, chart confirmation, sentiment confirmation, and delta availability.

Each contract receives:

- `liquidity_grade`
- `risk_grade`
- `trade_grade`

Important: this is a **candidate ranking layer**, not a full expected-value model. It does not yet include IV rank, volatility skew, earnings/event risk, theta/day, strategy-specific probability of profit, or option premium backtesting.

### AI Gate

```yaml
ai:
  enabled: true
  model: gpt-5.6
  timeout_seconds: 20
```

The AI gate is intentionally conservative:

- it uses only the JSON facts supplied by the app;
- it returns only `PROCEED` or `DO_NOT_PROCEED`;
- missing OpenAI configuration returns `DO_NOT_PROCEED`;
- failed OpenAI requests return `DO_NOT_PROCEED`;
- deterministic blockers are checked before the model is called.

If the answer is `DO_NOT_PROCEED`, the backend returns a concrete 3-4 sentence explanation.

### Ticker Profiles

```yaml
ticker_profiles:
  enabled: true
  backfill_on_add: true
  refresh_on_profile_view: false
  backfill_period: 3y
  backfill_intervals:
    - 15m
    - 1d
  keep_profile_when_removed_from_watchlist: true
```

When a symbol is added to the watchlist, the backend creates or reuses a `ticker_profiles` row, refreshes derived statistics from existing SQLite data, and queues the existing resumable historical backfill for the configured intervals. The profile stores data coverage, historical price behavior, indicator summaries, setup-family statistics, Fibonacci-related setup context, recent news/catalyst snapshots, recent options-positioning snapshots, and sample-backed ticker personality statements.

Profiles are incremental. The app appends new candles, setup records, news snapshots, and options snapshots over time; it does not discard the prior profile just because a page reloads.

### Decision Dashboard

```yaml
decision_dashboard:
  core_universe:
    - AAPL
    - MSFT
    - NVDA
    - AMZN
    - GOOGL
    - META
    - TSLA
    - PLTR
    - SPCX
    - CRM
    - CAT
    - JPM
    - PANW
    - CRWD
  require_profile_complete: true
  require_fibonacci_behavior: true
  require_news_current: true
```

The main dashboard is a decision page, not a raw-data page. It loads from stored ticker profiles, cached setup records, latest scans, news snapshots, and options-positioning snapshots first. It does not queue history backfills or block on provider calls during page render.

A ticker can appear as `Best Long Setup` or `Best Short Setup` only when deterministic hard gates pass: profile ready, required history present, current setup available, Fibonacci behavior analyzed, sufficient historical sample, positive expected value, current news state, options-chain snapshot, and validated contract context. Incomplete tickers are shown as `PROFILE BUILDING` or `DATA INCOMPLETE`, and the dashboard is allowed to show `No qualified long setup` or `No qualified short setup`.

### Advisory

```yaml
advisory:
  enabled: true
  deterministic_only: false
  model: gpt-5.6
  fallback_model: gpt-5.4-mini
  reasoning_effort: high
  advisory_mode: standard
  max_output_tokens: 1400
  timeout_seconds: 45
  maximum_calls_per_hour: 20
  cache_duration_seconds: 1800
  maximum_advisory_cost: 5.0
  prompt_version: trade-advisory-v1
  response_schema_version: advisory-response-v1
```

The advisory layer is not a probability engine. Deterministic code builds the market/session/setup/options/news package first, then the model explains the package in a strict JSON schema. Validation rejects unsupported probabilities, nonexistent contracts, generic disclaimer boilerplate, guarantee language, and attempts to override hard gates such as insufficient sample size, no acceptable contract, stale data, or non-positive expected value.

Admin users can update advisory model, fallback model, reasoning effort, token budget, timeout, cache duration, and deterministic-only mode from the `Settings` tab. Advice is cached by ticker, candidate, setup version, market-data version, option-chain version, news version, model, prompt version, and analysis version.

### Earnings history in ticker profiles

Ticker profiles persist quarterly earnings history for the configured lookback (one year by default). Alpha Vantage's `EARNINGS` endpoint is preferred, with the configured Finnhub fallback used when available. Each report records reported versus estimated EPS and revenue, classifies the result as `BEAT`, `MISS`, `MIXED`, `IN_LINE`, or `UNKNOWN`, and records report timing.

The profile also measures the stock reaction from stored daily candles: the opening gap, first-session return, three-session and five-session returns, and maximum favorable/adverse movement when those candles exist. Missing historical prices are shown as unavailable rather than estimated. Raw provider responses are cached under `data/cache`; profiles reuse that cache and only refresh it after the configured TTL.

### Social narrative intelligence

Social intelligence is an optional supporting signal. It reads only sources explicitly configured in `config/config.yml` under `social.sources`: `rss` sources use public feeds, while `json` sources are intended for authenticated APIs using a token referenced by `token_env`. The application does not bypass authentication, robots restrictions, paywalls, or rate limits.

Normalized mentions are stored in SQLite with hashed author identifiers, deduplicated discussion groups, stance, topics, relevance, spam risk, and source metadata. Profile summaries include sentiment, mention velocity, unique authors, source diversity, price/options confirmation, representative source links, and historical spike reactions. Social intelligence is capped as a small score contribution and cannot override hard trade gates, liquidity, expected value, stale data, or risk limits. Unconfigured or unavailable sources are reported as unavailable.

### Paper options capital and exits

The E*TRADE position view also calculates paper-portfolio controls from `paper_portfolio` settings. The 75% deployment value is a premium-committed cap with a 25% reserve; it is not an acceptable-loss limit. Realistic risk adds modeled spread, slippage, gap, IV-contraction, and liquidity-failure effects.

Long-option profit protection activates at a conservative executable return of +15% and ratchets a 5% trail from the highest executable bid. The first theoretical protected return is approximately +9.25% before slippage and gap risk. Losing positions are marked for same-day liquidation and are never approved for overnight holding. Green positions require a 70% estimated overnight probability, at least 30 independent examples, sufficient DTE, and acceptable liquidity before `HOLD OVERNIGHT` can appear. All trail and liquidation decisions are recorded in `paper_position_risk_states` and `paper_risk_audit_events`.

### Providers

```yaml
data:
  quotes_provider: etrade
  options_provider: etrade
  candles_provider: yahoo
  historical_candles_provider: alphavantage
  historical_candles_provider_fallback: finnhub
  quotes_provider_fallback: finnhub
  options_provider_fallback: none
  candles_provider_fallback: finnhub
  fallback_provider: yahoo
  cache_enabled: true
  backtest_mode: auto
```

```yaml
alphavantage:
  base_url: https://www.alphavantage.co/query
  timeout_seconds: 20
  output_format: json
  daily_outputsize: full
  adjusted_outputsize: full
  intraday_outputsize: full
  intraday_extended_hours: true
  intraday_adjusted: true
  daily_prefer_adjusted: true
```

Supported provider names:

- `etrade`
- `alphavantage` for quotes and historical candles
- `finnhub` for quotes and historical candles
- `stooq` for historical candles
- `yahoo`
- `twelvedata` for candles
- `none` for fallback fields

Free market-data providers are rate-limited, so the app caches provider responses in SQLite and on disk, skips ranges that already exist, and falls back to the next configured provider instead of hammering the same endpoint repeatedly.

Open E*TRADE positions are loaded with priority, and the backend will try multiple historical providers so the dashboard can build out more intraday and daily back data over time instead of relying on a single shallow slice.

## Running Commands

```bash
# Build images
docker compose build

# Start services
docker compose up -d

# Recreate backend after config/provider changes
docker compose up -d --force-recreate backend

# Logs
docker compose logs -f backend
docker compose logs -f frontend

# Stop services
docker compose down
```

## API Reference

Common endpoints:

```text
GET    /api/health
GET    /api/config
GET    /api/cache/candles/status
GET    /api/providers/status
GET    /api/db/status

GET    /api/auth/etrade/status
GET    /api/auth/etrade/connect
GET    /api/auth/etrade/callback
POST   /api/auth/etrade/verify
POST   /api/auth/etrade/disconnect

GET    /api/watchlist
POST   /api/watchlist
DELETE /api/watchlist/{symbol}

GET    /api/dashboard/decision
GET    /api/watchlist/intelligence
GET    /api/ticker-profiles/{symbol}
POST   /api/ticker-profiles/{symbol}/refresh
POST   /api/advisory/{symbol}
GET    /api/admin/advisory/settings
POST   /api/admin/advisory/settings

GET    /api/quote/{symbol}
GET    /api/candles/{symbol}
GET    /api/indicators/{symbol}
GET    /api/scan
GET    /api/scan/{symbol}
POST   /api/scan/run

GET    /api/options/{symbol}
GET    /api/options/{symbol}/ratios
GET    /api/options/{symbol}/contracts
POST   /api/ai/trade-gate
GET    /api/backtest/{symbol}
GET    /api/backtest/summary/{symbol}
POST   /api/history/backfill
GET    /api/history/backfill/status
POST   /api/history/backfill/cancel
GET    /api/alerts
```

Example Alpha Vantage-backed requests:

```bash
curl http://localhost:8000/api/quote/AAPL
curl "http://localhost:8000/api/indicators/AAPL?interval=5m&period=5d"
curl "http://localhost:8000/api/candles/AAPL?interval=1d&period=1y"
```

## Scanner Logic

The directional scanner compares long and short scores.

Long points can come from:

- close above VWAP;
- fast EMA above slow EMA;
- slow EMA above trend EMA;
- MACD histogram positive and rising;
- RSI in `45-68`;
- volume above average;
- close above Bollinger midpoint;
- current high above prior high.

Short points can come from:

- close below VWAP;
- fast EMA below slow EMA;
- slow EMA below trend EMA;
- MACD histogram negative and falling;
- RSI in `32-55`;
- volume above average;
- close below Bollinger midpoint;
- current low below prior low.

Grades:

```text
0-3  NO_TRADE
4-5  WATCH
6-7  TRADE_CANDIDATE
8+   HIGH_CONVICTION
```

## Options Ranking and Trade Gate Logic

The `/api/options/{symbol}/contracts` endpoint:

1. Fetches quote and configured expirations.
2. Removes expired expirations.
3. Fetches option chains.
4. Filters invalid contracts.
5. Fetches current chart signal and options sentiment.
6. Scores remaining contracts across liquidity, risk, and trade quality.
7. Returns separate ranked `calls` and `puts`.

Contracts are penalized for:

- `CLOSING`, `DELAYED`, or `SANDBOX` quote types;
- stale quote timestamps;
- `0DTE` expiration risk;
- spread above `5%`;
- low volume;
- low open interest;
- far-OTM positioning;
- unavailable delta;
- chart signal not confirming the contract direction.

The UI labels contracts as **Top Liquid Call Candidates** or **Top Liquid Put Candidates** unless chart signal confirms the directional bias.

The UI separates gate status into:

- Chart Signal
- Historical Edge
- Option Liquidity
- Data Quality
- AI Gate
- Final Decision

`Final Decision` is limited to `NO_TRADE`, `WATCH`, `WAIT_FOR_CONFIRMATION`, `TRADE_CANDIDATE`, or `HIGH_CONVICTION`.

The UI can show `TRADE_CANDIDATE` or `HIGH_CONVICTION` only after:

- chart signal grade is `TRADE_CANDIDATE` or `HIGH_CONVICTION`;
- options sentiment confirms or is neutral;
- liquidity grade is `A` or `B`;
- quote is not stale;
- spread is at or below `recommended_max_spread_pct`;
- volume is at or above the configured minimum;
- historical win rate is at least `52%`;
- contract type matches the directional bias (`LONG` uses `CALL`, `SHORT` uses `PUT`);
- the AI gate returns `PROCEED`.

## Data Freshness

- Quote cache default: `10` seconds.
- Candle cache default: `60` seconds.
- Option expiration cache default: `86400` seconds, but expired dates are filtered before use.
- Option chain/ranking cache default: `60` seconds.
- Scan cache default: `60` seconds.

Provider data can be delayed, incomplete, or rate-limited. E*TRADE and Yahoo may differ materially in quote status, chain fields, Greeks, and timestamps.

## Backtesting

Backtesting is setup-level, not full option premium simulation.

Current behavior:

- finds historical technical setups matching side and score threshold;
- simulates next-open entry to same-day close on the underlying;
- reports occurrence count, wins, win rate, sample confidence, historical edge, and sample trades;
- prefers candles already stored in SQLite before calling a provider;
- falls back to scan-history approximation when historical candles are unavailable.

Limitations:

- does not reconstruct historical option chains;
- does not model fills, slippage, bid/ask movement, IV changes, theta decay, or assignment/exercise;
- should be treated as context, not proof of edge.

## Historical Backfill and Rate Limits

Rate limits are expected with free or retail providers. The app avoids repeated provider calls by storing candle history in SQLite, checking stored ranges before each fetch, and chunking historical backfills.

Historical intraday availability may be limited. For example, providers may return only part of a requested `5m` or `15m` range. The app stores whatever is returned and continues building history over time from future scans and slow backfills.

Stooq is configured as the default historical candle provider because it does not require an API key. If Stooq returns browser verification or a captcha page instead of CSV, the app treats that as a provider cooldown and pauses rather than bypassing the verification or hammering the provider.

Backfill behavior:

- intraday intervals are chunked, defaulting to 7-day chunks;
- daily history can use larger chunks, defaulting to 365 days;
- `1m` history is not included by default;
- incomplete backfill chunks can be resumed;
- provider requests are throttled and rate-limit errors use exponential backoff with jitter;
- OpenAI is never called during historical backfill.

Run a slow backfill:

```bash
curl -X POST http://localhost:8001/api/history/backfill
```

Inspect backfill status:

```bash
curl http://localhost:8001/api/history/backfill/status
```

Inspect provider/rate-limit status:

```bash
curl http://localhost:8001/api/providers/status
```

Adding a ticker to the watchlist also queues a profile backfill using the configured ticker-profile period and intervals. The backfill worker still checks SQLite coverage first, skips stored ranges, honors provider cooldowns, and resumes incomplete chunks.

## Local Development

Backend:

```bash
cd backend
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
```

Frontend:

```bash
cd frontend
npm install
npm run dev
```

Build check:

```bash
cd frontend
npm run build
```

Python syntax check:

```bash
python3 -m compileall backend/app
```

## Historical Setup Matching

The dashboard includes a deterministic `Historical Setup Match` panel for current long and short candidates. It compares the latest completed 15-minute setup with prior stored SQLite candles for the same symbol and, separately, comparable watchlist setups.

Key rules:

- Probabilities come from stored historical examples, not GPT.
- Same-symbol evidence and watchlist-wide evidence are displayed separately.
- Samples under 10 examples are labeled insufficient.
- Outcomes are calculated forward from the setup timestamp only; future candles are not used to define the setup.
- Overlapping candles from the same move are deduplicated so one trend does not become many fake wins.
- Option contracts still must pass liquidity, spread, freshness, and structure gates.

Queue the slow provider-safe history warmup:

```bash
curl -X POST http://localhost:8001/api/history/setup-match/backfill
```

Inspect a symbol match:

```bash
curl http://localhost:8001/api/history/setup-match/AAPL?side=LONG&interval=15m&period=3y
```

The default backfill period is now `3y`. Intraday availability still depends on the provider; when a provider cannot supply three years of 15-minute data, the app stores the maximum available and continues building history over time.

## Profile Completeness

Ticker readiness is evaluated server-side after profile refreshes, backfills, startup
reconciliation, and profile requests. The staged states are:

`NOT_STARTED`, `BUILDING`, `PARTIAL`, `ANALYSIS_PENDING`, `READY_FOR_PLANNING`,
`READY_FOR_LIVE_ANALYSIS`, `STALE`, `BLOCKED`, and `ERROR`.

The evaluator persists component-level readiness for history, indicators, support/resistance,
Fibonacci, setup history, sample size, market regime, relative strength, news, options,
deterministic scoring, and live freshness. Missing values remain null or are labeled Pending,
Unavailable, Insufficient sample, Data stale, or Provider unavailable. A partial profile has
no numeric score and cannot enter the dashboard’s Top Long or Top Short rankings.

SQLite retention maintenance:

```bash
sqlite3 data/indicator.db < sql/retention.sql
```

## Recommendation Performance

Every material dashboard candidate is stored as an immutable recommendation snapshot in
`recommendation_records`. The lifecycle is `CREATED` -> `TRIGGERED` -> `RESOLVED`; a
recommendation that never triggers is tracked separately and is excluded from trade win
rates. The original deterministic inputs are retained in `snapshot_json`, while lifecycle
changes are append-only in `recommendation_events`.

Performance is available on the dashboard and Paper Portfolio tabs, or through:

```bash
curl http://localhost:8001/api/recommendations/performance
```

For paper-trading evaluation, an admin can record lifecycle events without changing the
original recommendation:

```bash
curl -X POST http://localhost:8001/api/recommendations/<id>/trigger \
  -H 'Content-Type: application/json' \
  -d '{"entry_price": 123.45, "option_entry_price": 2.10}'

curl -X POST http://localhost:8001/api/recommendations/<id>/resolve \
  -H 'Content-Type: application/json' \
  -d '{"outcome":"WIN", "realized_pnl":436, "directional_correct":true, "target_before_invalidation":true, "profitable_option":true}'
```

The full-trade win rate uses resolved triggered recommendations only. Created-but-never-
triggered, invalidated-before-entry, active, and unresolved recommendations are not counted
as wins or losses.

## Real E*TRADE Versus Paper Trading

These are separate systems. `REAL E*TRADE` reads broker accounts and positions from the
E*TRADE integration and is read-only analysis. It does not run paper risk controls or show
simulated fills. `PAPER CHALLENGE` uses its own `$100,000` portfolio, orders, fills,
positions, recommendation ledger, and performance snapshots.

Separate API families enforce the boundary:

```text
/api/etrade/accounts
/api/etrade/positions
/api/etrade/orders
/api/etrade/trades

/api/paper/portfolio
/api/paper/positions
/api/paper/orders
/api/paper/recommendations
/api/paper/performance
```

Paper order IDs are generated with a `paper-` prefix and brokerage identifiers are rejected.
Legacy generic recommendation rows are copied into the paper ledger. Legacy risk rows whose
provenance is ambiguous are retained and listed at `/api/admin/paper/migration-review` for
admin review rather than being silently assigned to either system.

## Troubleshooting

### E*TRADE says not connected

- Confirm `.env` contains valid consumer key and secret.
- Confirm the callback URL matches your E*TRADE app exactly.
- Visit `/api/auth/etrade/status`.
- Reconnect from the `Settings` tab.

### No option candidates

Likely causes:

- all contracts failed volume/OI/spread filters;
- provider returned missing bid/ask;
- E*TRADE auth expired;
- the symbol has poor options liquidity;
- selected expirations are too near or unavailable.

Check the `warnings`, `filtered_counts`, and `filtered_out_count` fields from `/api/options/{symbol}/contracts`.

### Yahoo candle issues

- Yahoo/yfinance can rate-limit or return delayed/incomplete data.
- Use `/api/providers/status` for rate-limit/backoff state.
- Use `/api/history/backfill/status` and `/api/db/status` to confirm SQLite history is being reused.
- Use the candle cache worker status endpoint for live cache warming: `/api/cache/candles/status`.
- Consider Twelve Data for candles if Yahoo is unreliable.

## Roadmap

High-value next improvements:

- Add IV rank / IV percentile and skew-aware scoring.
- Add event filters for earnings, dividends, FOMC/CPI, and major market events.
- Backtest option premium behavior, not only underlying direction.
- Add strategy-specific scoring for scalps, day trades, swings, debit spreads, and hedges.
- Add configurable risk sizing and max premium-at-risk displays.
- Add tests around provider normalization and option filtering.

## Disclaimer

Trading options involves substantial risk and can result in total loss of premium or more depending on strategy. This software is an analytical dashboard only. Validate all data and decisions independently before trading.
