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
