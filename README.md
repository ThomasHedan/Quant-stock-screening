# Momentum Gap Scanner (v0.6)

A self-hosted web app that **screens, records and notifies** on US small-cap
pre-market and post-market momentum. It never places orders and it gives no
trading advice.

Three jobs:

1. **Scan** — poll US stocks during pre/post-market alert windows, apply the
   Warrior Trading 5 Pillars, check for a fresh news catalyst, and send browser
   push notifications.
2. **Record** — collect a far broader sample of market data than the alerts use,
   into a Parquet + DuckDB research lake, so screening rules that have not been
   thought of yet can still be tested later.
3. **Review** — at the end of each day, find the big runners the scanner missed,
   explain *why* it missed them, and keep them on the radar for a few days.

Full specification: [`CLAUDE.md`](CLAUDE.md). Design decisions and their
rationale: [`DECISIONS.md`](DECISIONS.md).

## Status

All eleven build-order steps are implemented, tested and tagged
(`step-00` … `step-11`).

| Step | Scope | State |
|---|---|---|
| 0 | Repo skeleton, `.gitignore`, ruff + pytest config | ✅ |
| 1 | Config, SQLite, pure `core/` functions + tests | ✅ |
| 2 | Parquet lake writer, compaction, `reference` split, quality table | ✅ |
| 3 | TradingView broad collector | ✅ |
| 3b | Tier 0 daily universe, move metrics, pruning, retention | ✅ |
| 3c | Integrity layer (corporate actions, halts, float confidence) | ✅ |
| 4 | Alpaca news listener + backfill | ✅ |
| 5 | Alert windows: pillars, tiering, evaluations | ✅ |
| 6 | Web Push + service worker + test button | ✅ |
| 7 | Live / History / Settings pages | ✅ |
| 8 | Alpaca bars: RVOL baseline, outcomes | ✅ |
| 9 | Runner detection, Missed Runners page | ✅ |
| 10 | Trade journal | ✅ |
| 11 | Research page, starter notebook, mock mode | ✅ |

**Not yet verified against the live APIs.** This build ran in an environment
with no access to `scanner.tradingview.com`, `api.alpaca.markets` or
`finnhub.io`, so every external call is covered by mocked-transport tests
rather than a real response. `DECISIONS.md` lists exactly what to check on the
first live run — chiefly the TradingView column names and that Alpaca returns
extended-hours bars.

## Requirements

- Python 3.12
- A machine that can stay up 04:00–20:45 ET (a small VPS, or a home box behind
  a Cloudflare Tunnel). Serverless platforms are **not** suitable: the app needs
  a long-lived scheduler, a news WebSocket and a local data lake.
- Free API accounts: [Alpaca](https://alpaca.markets) (market data + news),
  optionally [Finnhub](https://finnhub.io) as a news fallback.
- About 6 GB of disk per year of collection (see `/research` for live figures).

## Setup

```bash
python3.12 -m venv .venv
source .venv/bin/activate
pip install -r requirements-dev.txt

cp .env.example .env            # then fill in your keys
```

Generate a VAPID key pair for push:

```bash
python -c "from py_vapid import Vapid01; v = Vapid01(); v.generate_keys(); \
  print('public :', v.public_key); print('private:', v.private_key)"
```

Run it:

```bash
uvicorn app.main:create_app --factory --host 0.0.0.0 --port 8000
```

Secrets live in `.env` only and are never logged. `config.yaml` holds every
threshold, window and cadence — there are no magic numbers in the code.

### Try it without waiting for market hours

```bash
MOCK_DATA=1 uvicorn app.main:create_app --factory --port 8000
```

Mock mode serves a deterministic synthetic market containing a few complete
five-pillar setups, a runner that moves at 07:20 ET (outside every alert
window), a runner with a 23M float, an illiquid runner nobody could have
traded, and several dozen names that went nowhere. It exercises the whole
pipeline — evaluation, push, lake writes, missed-runner diagnosis — at any hour.

## Deployment

HTTPS is required for Web Push everywhere except `localhost`.

**Small VPS.** Run behind nginx or Caddy with a certificate; point it at
uvicorn on `127.0.0.1:8000`. Keep the process under systemd so it restarts
after a reboot — an unattended scanner that quietly died is worse than none.

**Home machine + Cloudflare Tunnel.** `cloudflared tunnel --url
http://localhost:8000` gives a public HTTPS hostname with no port forwarding,
which is enough for push to work.

**iPhone.** Web Push only reaches an iOS device when the site is installed to
the home screen: open it in Safari, Share → *Add to Home Screen*, then enable
notifications from the installed app. The Settings page says the same thing at
the point of use.

## Daily rhythm (ET)

| Time | What runs |
|---|---|
| 04:00–20:00 | News WebSocket, with backfill on every reconnect |
| 07:00–10:30, 15:30–17:00 | Broad collector, every 60 s |
| 04:00–07:00, 10:30–15:30, 17:00–20:00 | Broad collector, every 5 min |
| 08:00, 08:30, 09:00, 16:00, 16:30 (5 min each) | Alert windows, every 30 s |
| 11:05 | Pre-market outcomes and missed runners |
| 20:10 | Corporate actions |
| 20:15 | Tier 0 universe, move metrics, full-day outcomes, runners |
| 20:45 | Pruning, compaction, retention, data quality |

Holidays and early closes come from the XNYS calendar; a half day drops the
16:00 and 16:30 windows automatically.

## The pages

- **Live** — the current window's table: price, gap, window change, RVOL (with
  its source), float (greyed when confidence is low), five pillar dots, tier,
  recent-runner badge, and one-tap Traded / Skipped / Note buttons.
- **Missed Runners** — what ran, whether it was caught, and why not, with
  miss-reason chips and the recent-runner watchlist.
- **History** — past alerts by date, with their outcomes once computed.
- **Journal** — the day's logged decisions.
- **Research** — lake size per table, the holdout cutoff, and a read-only
  DuckDB query box.
- **Settings** — the thresholds in force, push enable/disable and a test push.

## Development

```bash
ruff check .
ruff format --check .
pytest
mypy
```

All four must pass before a commit. `main` is always green.

### Timezones

Everything is stored and computed in timezone-aware **UTC**. Scheduling is
expressed in `America/New_York`; display converts to ET and the viewer's local
zone. A naive `datetime` anywhere in this codebase is a bug, and the lint rules
enforce it.

### Point-in-time discipline

Any function computing a metric "as of" a moment takes an explicit `as_of`
argument and must not read data timestamped after it. Lookahead bugs produce
research that looks excellent and loses money.

### Reading the lake

Start with [`research/starter.ipynb`](research/starter.ipynb). It works one
question end to end and demonstrates the three corrections that decide whether
a number from this lake means anything: re-weighting the control sample,
reporting tradeable and all-rows figures side by side, and excluding
`suspect_price` and `spans_halt` rows.

The single easiest mistake is forgetting the control re-weighting. Tier 1 keeps
every mover but only a 10% sample of everything else, so an unweighted rate has
the wrong denominator and looks roughly ten times better than reality.

---

*This tool screens stocks, records data and sends alerts. It does not provide
financial advice.*
