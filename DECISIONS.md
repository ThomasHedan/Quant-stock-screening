# DECISIONS.md

Every ambiguous requirement resolved the safer way, every dependency added, and
every data quirk worked around gets a dated entry here. In six months this file
plus the git log is the only record of *why*.

Format: `## YYYY-MM-DD — <short title>` / **Context** / **Decision** /
**Trade-off**.

---

## 2026-09-18 — Python 3.12 in a local venv

**Context.** `CLAUDE.md` §3 pins Python 3.12. The build host defaults to
Python 3.11, with 3.12 available at `/usr/bin/python3.12`.

**Decision.** The project venv is built from `/usr/bin/python3.12`; `ruff` and
`mypy` both target `py312`. Modern syntax (`str | None`, `list[str]`) is used
unconditionally.

**Trade-off.** Anyone cloning this needs 3.12+; that is already the stated
requirement, so no compatibility shims are carried.

---

## 2026-09-18 — One working branch instead of one branch per step

**Context.** `CLAUDE.md` §1.2 asks for one branch per build-order step, merged
to `main` with `--no-ff` and tagged. This build runs in an environment whose
operating rules restrict pushes to a single designated branch
(`claude/add-file-start-work-csjrad`).

**Decision.** Keep every other part of the git discipline exactly as specified —
small single-purpose commits, Conventional Commits messages, a `step-NN` tag at
the end of each step, tests green before each commit — but land the commits on
the designated branch rather than merging a step branch per step.

**Trade-off.** The history loses the `--no-ff` merge bubble that groups a step's
commits visually. The `step-NN` tags preserve the same boundaries, so
`git log step-01..step-02` still reads as one step, and bisect is unaffected.
Reverting a whole step is a range revert rather than a single merge revert.

---

## 2026-09-18 — Dependency baseline

**Context.** §1.1: no new dependency without a line here saying why the stdlib
was not enough.

**Decision.** The initial set, each with its justification:

| Package | Why not stdlib |
|---|---|
| `pydantic` / `pydantic-settings` | Validation at boundaries (§1.1). `dataclasses` do not validate, coerce or report field-level errors. |
| `pyyaml` | `config.yaml` is required by §1.1; stdlib reads JSON and INI only. |
| `pyarrow` | Parquet writer with zstd + dictionary encoding (§6.3a). No stdlib Parquet. |
| `duckdb` | Research SQL over the lake (§6.7). Nothing in stdlib queries Parquet. |
| `exchange_calendars` | XNYS holidays and early closes (§3). Encoding the NYSE calendar by hand is a recurring source of silent bugs. |
| `fastapi` + `uvicorn` + `jinja2` | Stack fixed by §3. |
| `apscheduler` | Cron-like jobs in-process with a market calendar (§3). `sched`/threads would mean rewriting misfire handling and timezone-aware triggers. |
| `httpx` | Timeouts and connection pooling for every external call (§1.1). `urllib` has no pooling and an awkward timeout story. |
| `websockets` | Alpaca news stream (§5.5). |
| `pywebpush` | VAPID-signed Web Push (§8); the crypto is not something to hand-roll. |
| `tradingview-screener` | Snapshot source fixed by §3. |
| `pandas` | Transitive requirement of `exchange_calendars` and `tradingview-screener`; used only at the edges, never inside `core/`. |
| `pytest` / `ruff` / `mypy` | Test and lint gates fixed by §1.1. |

**Trade-off.** `pandas` and `pyarrow` are heavy, but both come with the required
stack anyway. `core/` stays free of them so the pure logic remains trivially
testable.

---

## 2026-09-18 — Pinned tooling notes for this build host

**Context.** PyPI is reachable from this build environment only through the
agent proxy; the default `no_proxy` sends pip direct, where it times out.

**Decision.** Installs run as
`env -u no_proxy -u NO_PROXY pip install --proxy "$HTTPS_PROXY" …`. Nothing in
the application depends on this; it is a build-host note only, recorded so the
next person does not conclude the package set is wrong.

**Trade-off.** None for the app. The workaround is absent from
`requirements.txt`, which stays portable.

---

## 2026-09-18 — Absent news is a pillar-3 FAIL, not UNKNOWN

**Context.** §5.4 says missing data makes a pillar *unknown*. Pillar 3 is a
special case: "no article in the last 15 minutes" can mean either *there is no
catalyst* or *the feed is down*, and those deserve opposite verdicts.

**Decision.** `check_news_catalyst(None, …)` returns `FAIL`, documented as "a
connected, quiet feed is information". A caller that knows the feed was
disconnected must not call it and must record the pillar `unknown` itself;
WebSocket downtime is already tracked in `data_quality` (§6.6), so the caller
always has the information it needs to tell the two apart.

**Trade-off.** A caller that forgets to check feed health will record a false
`FAIL` during an outage. That is the safer direction — it suppresses alerts
rather than inventing them — and the outage is visible in `data_quality`.

---

## 2026-09-18 — rank_score renormalises over the components present

**Context.** §5.2 defines `rank_score` as a fixed weighted sum of three ranks.
Early in a window, RVOL is often still `None` for tickers whose baseline has not
been computed yet.

**Decision.** Missing components are dropped and the remaining weights are
rescaled, rather than substituting a rank of zero.

**Trade-off.** Scores computed from different component sets are not strictly
comparable. Substituting zero was worse: it would push exactly the newest,
fastest-moving names to the bottom of the table, which is the opposite of what
the ranking exists for. Rows record which components were present.

---

## 2026-09-18 — Two modules beyond the §10 layout: `schemas.py`, `reference.py`

**Context.** §10 lists `storage/lake.py`, `storage/db.py`, `storage/quality.py`.
Two concerns did not fit cleanly in any of them.

**Decision.** `storage/schemas.py` holds the Arrow schema of every lake table,
and `storage/reference.py` holds the change-only writer and the point-in-time
join for slow-moving fields.

**Trade-off.** Two files more than the spec's layout. The alternative was a
single `lake.py` of roughly 900 lines mixing table definitions, buffering,
compaction and join semantics. Schemas in particular earn their own file: they
are the documentation a DuckDB query needs months from now, and `NEVER_PRUNED`
sits next to them so the retention exclusion list cannot drift from the tables
it protects.

---

## 2026-09-18 — `market_cap` is excluded from reference change detection

**Context.** §6.3a lists `sector`, `industry`, float and average-volume fields
as the slow-moving ones written change-only. `market_cap` is stored on the same
rows.

**Decision.** `market_cap` is written on each reference row but is *not* part
of the comparison that decides whether to write one.

**Trade-off.** Market cap is only as fresh as the last row some other field
triggered. Including it would have produced a reference row on every poll,
which is exactly the duplication the snapshots/reference split exists to
remove — and market cap is derivable from price × shares outstanding anyway.

---

## 2026-09-18 — Step tags are local only

**Context.** §1.2 requires a `step-NN` tag per completed step. This
environment's git proxy rejects tag pushes (HTTP 403); branch pushes succeed.

**Decision.** Tags are created locally at each step boundary as specified. They
will need a one-off `git push --tags` from a machine without that restriction.

**Trade-off.** Until then, step boundaries are visible in the log through the
commit messages and this file rather than through `git tag -l` on the remote.

---

## 2026-09-18 — TradingView field names are unverified against the live endpoint

**Context.** §6.4 asks that extended-hours behaviour and field availability be
verified at build time. This build environment's egress policy blocks
`scanner.tradingview.com` (the proxy returns 403), so no live response could be
inspected.

**Decision.** The column list in `app/sources/tradingview.py` is written against
the documented `tradingview-screener` column names, and every assumption about
it is enforced at runtime rather than trusted: a missing essential column
(`name`, `close`, `volume`) raises `FieldDriftError` and fails the poll; a
missing optional column is logged as an error, recorded on the poll result, and
marks the affected pillar unknown; a positional-length mismatch between the
requested columns and the returned values raises immediately.

**Trade-off.** The first live run on the trader's machine may reveal a renamed
column. It will fail loudly and name the column, which is the intended
behaviour — the alternative, silently defaulting, is what this project exists
to avoid. Still to verify on a machine with network access: that extended-hours
Alpaca bars are returned (§5.3), and that `premarket_*` / `postmarket_*` columns
populate as expected outside regular hours.

---

## 2026-09-18 — Float confidence from a snapshot tops out at `medium`

**Context.** §6.4.4 derives `float_confidence` from turnover, the age of
`float_asof`, and nullness. TradingView snapshots carry no as-of date for the
float figure.

**Decision.** The collector passes `float_asof=None`, so a plausible float earns
`MEDIUM`, never `HIGH`. Only a source that supplies an as-of date can promote it.

**Trade-off.** Pillar 5 still evaluates normally on `MEDIUM` (only `LOW` forces
unknown), so this changes no alert today. It keeps the confidence label honest
about what is actually known, and leaves `HIGH` meaningful for when a dated
source is wired in.

---

## 2026-09-18 — `app/collector.py` and `app/core/collection.py`

**Context.** §10's layout has sources, pure core modules and storage, but no
home for the glue that turns a snapshot into lake rows, nor for the loose live
filter.

**Decision.** The filter is a pure module in `core/` (`collection.py`) because
it is arithmetic over explicit inputs and deserves the same test discipline as
the pillars. The glue is `app/collector.py`, which holds the window baselines
and is driven by an explicit `now`.

**Trade-off.** Two more modules than the spec's tree. The alternative was
putting filter logic in `sources/tradingview.py`, which would have tied a
threshold decision to one vendor and made it untestable without a fake
response.

---

## 2026-09-18 — Halts are inferred only while the broad market is active

**Context.** §6.4.3 infers a halt from zero trades for ≥3 consecutive minutes
"during an active session while the broad market is trading". Alpaca's free
tier omits empty minutes rather than emitting zero-volume bars, so silence and
a halt look identical in the data.

**Decision.** `infer_halts` takes an explicit `market_active` flag and returns
nothing when it is false. The caller decides, from the market calendar and the
clock, whether the broad market was trading.

**Trade-off.** Genuine pre-market halts are not inferred. The alternative was
worse by a wide margin: a thin small cap is routinely silent for twenty minutes
before the open, so inferring there would mark most of pre-market as halted and
make `spans_halt` useless as an exclusion filter.

---

## 2026-09-18 — Split ratios are stored as new-shares-per-old-share

**Context.** Alpaca reports `old_rate` and `new_rate`; the lake stores a single
`ratio`.

**Decision.** `ratio = new_rate / old_rate`. A 1:10 reverse split is `0.1`
(ten old shares become one), a 3-for-1 forward split is `3.0`. Prices are
divided by the ratio and volumes multiplied by it.

**Trade-off.** The convention has to be remembered when reading the table. It
is the one that makes `is_reverse_split` a plain `ratio < 1` test and keeps the
adjustment arithmetic in one direction.

---

## 2026-09-18 — Alpaca endpoints are unverified against the live API

**Context.** As with TradingView, the egress policy blocks `api.alpaca.markets`
and `data.alpaca.markets` from this build environment.

**Decision.** The corporate-actions client is written against the documented
v1 endpoint shape and fully tested against `httpx.MockTransport`, covering the
page cursor, a retried 503 and an unretried 401. Field names in the response
are parsed defensively: a record missing a date or symbol raises, and the rest
of the page still parses.

**Trade-off.** The first live run may show a differently named field. Still to
verify on a networked machine: the exact `corporate_actions` response keys, and
whether the free tier's 15-minute delay applies to this endpoint as it does to
market data.

---

## 2026-09-18 — `pytz` is pinned because DuckDB needs it for TIMESTAMPTZ

**Context.** Every lake timestamp is `TIMESTAMP WITH TIME ZONE`. Returning one
to Python from DuckDB raises `ModuleNotFoundError: pytz` — the driver converts
through `pytz` and does not declare it as a hard dependency.

**Decision.** `pytz` is listed in `requirements.txt` with this note. No
application code imports it; storing naive timestamps to avoid it was never an
option, since the whole codebase depends on timezone-aware UTC.

**Trade-off.** One more package, needed only by `/research` queries that select
a timestamp column — which is most of them.

---

## 2026-09-18 — The research query timeout is a watchdog interrupt

**Context.** §6.7 requires a 10-second timeout on the DuckDB query box. DuckDB
has no `statement_timeout` setting.

**Decision.** The query runs with a `threading.Timer` that calls
`connection.interrupt()` when the limit expires; the interrupt surfaces as a
rejected query with a clear message.

**Trade-off.** The timer fires against the whole connection rather than one
statement, which is exactly the granularity wanted here (one connection per
request). Without it, one careless join over a year of snapshots would hold the
page open indefinitely.
