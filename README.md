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

Under construction, following the build order in `CLAUDE.md` §12. Each step is
committed, tested and tagged (`step-00`, `step-01`, …).

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
| 6 | Web Push | ⏳ |
| 7 | Live / History / Settings pages | ✅ |
| 8 | Alpaca bars: RVOL baseline, outcomes | ✅ |
| 9 | Runner detection, Missed Runners page | ⏳ |
| 10 | Trade journal | ⏳ |
| 11 | Research page, starter notebook, mock mode | ⏳ |

## Requirements

- Python 3.12
- A machine that can stay up 04:00–20:45 ET (VPS, or a home box behind a
  Cloudflare Tunnel). Serverless platforms are **not** suitable: the app needs a
  long-lived scheduler, a news WebSocket and a local data lake.
- Free API accounts: [Alpaca](https://alpaca.markets) (market data + news),
  optionally [Finnhub](https://finnhub.io) as a news fallback.

## Setup

```bash
python3.12 -m venv .venv
source .venv/bin/activate
pip install -r requirements-dev.txt

cp .env.example .env             # then fill in your keys
```

Secrets live in `.env` only and are never logged. `config.yaml` holds every
threshold, window and cadence — there are no magic numbers in the code.

## Development

```bash
ruff check .
ruff format --check .
pytest
```

All three must pass before a commit. `main` is always green.

### Timezones

Everything is stored and computed in timezone-aware **UTC**. Scheduling is
expressed in `America/New_York`; display converts to ET and the viewer's local
zone. A naive `datetime` anywhere in this codebase is a bug.

### Point-in-time discipline

Any function computing a metric "as of" a moment takes an explicit `as_of`
argument and must not read data timestamped after it. Lookahead bugs produce
research that looks excellent and loses money.

---

*This tool screens stocks, records data and sends alerts. It does not provide
financial advice.*
