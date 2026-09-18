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
