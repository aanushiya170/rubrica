# ARCHITECTURE.md

## The one rule

Everything is built as layers on top of a T1+T2 core that never breaks.
`src/rubrica/core/` never imports `src/rubrica/extensions/`;
`tests/test_layering.py` enforces it and also boots the app with
`RUBRICA_EXTENSIONS=0` to prove the core runs alone. Delete `extensions/` and the
seven acceptance checks still pass.

```
                         RUBRICA
                               |
       +---------------+-------+------+---------------+
       |               |              |               |
  SUBMISSION        JUDGING        EVIDENCE          PUBLIC
   ENGINE            ENGINE         ENGINE           ENGINE
 events/teams     assignment      audit log        voting
 projects         rubric          replay/verify    comments
 deadlines        isolation       integrity        hidden results
 gallery          calibration     alerts           randomized ballots
                  normalization                    rate limits
                        |
                        v
                  RESULT ENGINE  (normalization run → published snapshot)
                        |
                        v
                 extensions/  api · webhooks · certificates · embeds · bulk · pairwise
```

## Stack

| layer | choice | why |
|---|---|---|
| runtime | Python 3.12, FastAPI, uvicorn | boring, fast to debug, typed request models where they help |
| database | SQLite (stdlib `sqlite3`, WAL) | zero extra container, relational integrity, one file to back up |
| views | Jinja2 server-rendered HTML, inline CSS | UI is not separately scored; no build step, no CDN |
| auth | hand-rolled: scrypt passwords, opaque session tokens in an HttpOnly cookie | the checker only needs a header; no auth-as-a-service allowed |
| container | one service | fewer moving parts on a fresh clone with the network off |

## Request path

```
request → resolve session (cookie or Bearer) → route → service function
        → scope check inside the service (auth.assert_judge_scope / require_staff)
        → SQL → audit.record → signals.emit → response
```

Identity always comes from the session. Query parameters like `?judge=` are
treated as *requests* that the scope check may refuse. This is why
`GET /api/judge/scores?judge=jdg_24` as judge_b is a 403 and not a template
that hides a column.

`core/http.py` maps `DomainError` subclasses to status codes in one place:
`Unauthorized→401`, `Forbidden/Closed→403`, `NotFound→404`, `Conflict→409`,
`RateLimited→429`. JSON callers get JSON errors; browsers get an error page (or
a redirect to login for 401).

## Modules

```
src/rubrica/
  app.py                 app factory; mounts core then extensions
  core/
    schema.sql           full schema (see DATA-MODEL.md)
    db.py                connection, schema apply, migration table
    auth.py              passwords, sessions, roles, scope checks
    audit.py             append-only log; every record is also a signal
    signals.py           tiny pub/sub; core emits, anyone subscribes
    seed.py              fixture loader (idempotent) + demo event + test logins
    http.py              actor resolution, error mapping, templates
    services/            events · teams · projects · judging · normalization
                         results · integrity · community · export
    api/routes.py        /api/* JSON + CSV (the checker's routes live here)
    web/                 HTML routers + templates
  extensions/
    api.py               /api/v1 (own OpenAPI doc, offline docs page, API keys)
    webhooks.py          HMAC-signed outbound webhooks, background worker
    certificates.py      participant certificates, Ed25519-signed judge records
    embeds.py            iframe gallery + script tag + CORS JSON
    bulk.py              fixture-shaped JSON import/export
    pairwise.py          Bradley–Terry pairwise judging
```

Extension modules expose `SCHEMA` (SQL appended at init), `register(app)` and
optional `on_startup/on_shutdown`. They import from core; core knows nothing
about them. The only bridge is `core/signals.py`: every audit action is emitted
as a signal, and webhooks subscribe to `*`.

## Two seeded events — and why

* **`evt_01` — the official fixture.** Loaded verbatim from `fixtures.json`:
  41 projects, 30 judges, 8 tracks, 40 teams, 126 scores, and the
  fixture's own `submissions_close` (2026-03-01T18:00Z, in the past). Marked
  `is_historical`, which makes it read-only for submissions regardless of
  clock. **Never mutated by the seed.** The acceptance checker runs against it
  and the normalization proof is computed on it.
* **`evt_02` — a live demo event.** Created by the seed with submissions open
  for 30 days and a voting window already open, two demo judges, two teams,
  one submitted and one draft project. It exists so the full submit → judge →
  publish lifecycle can be exercised (and recorded for the demo video) on an
  event whose deadline is not already in the past. It uses different ids, a
  different rubric, and a different name; nothing about it touches `evt_01`.

The split is documented here so nobody mistakes it for altering or hiding the
official fixture.

## Idempotent seed

`seed_all` skips the fixture load when `evt_01` exists, skips the demo event
when `evt_02` exists, and re-creates only the four fixed test sessions. Booting
twice yields exactly 41 fixture projects (asserted in the boot log and in
tests). The seed prints the four checker headers to stderr on every boot.

## Judging engine decisions

* **Blind overlap instead of disjoint batches.** See JUDGING.md §2. Overlap in
  *what* is reviewed is what makes calibration possible; the backend refuses to
  reveal *who* scored *what*.
* **Rubric versions are immutable.** A new rubric is a new version; scores
  keep `rubric_version`.
* **Normalization runs are first-class rows.** Parameters and per-judge stats
  are stored with the results so a run can be explained later.
* **Publishing freezes inputs, not just outputs.** `result_snapshots` stores
  the score ids, project ids, exclusions and parameters plus a SHA-256 of
  them. `verify` recomputes and diffs. No hash chain: recomputation from
  recorded evidence is the honest, checkable claim; a chain would add ceremony
  the checker never exercises.
* **Duplicates are resolved, not deleted.** `canonical_project_id` + status
  `excluded`. Scores stay as evidence; ranking drops the excluded record.

## Public engine (T3) decisions

* Votes require an account; one vote per (project, voter) is a UNIQUE
  constraint, and the duplicate attempt is itself audited.
* Ballot order is a deterministic shuffle seeded by `sha256(event:voter)`, so
  it is random across voters but stable across reloads for one voter.
* Results (rubric and vote totals) are hidden from non-staff while the voting
  window is open, and until an organizer publishes.
* Rate limits are a sliding window per user (and per client IP for votes) in
  process — right for one node, and documented as such.
* The audit log is plain and readable: `seq, timestamp, actor, action, entity,
  payload`, filterable in the UI and exportable as CSV.

## Operability

* `GET /health` for the compose healthcheck.
* Single volume `./data` holds the SQLite file and the Ed25519 signing key.
* Migration path in: `POST /api/v1/import/events` with a fixture-shaped
  document (the same loader the official fixture goes through).
* Migration path out: `GET /api/v1/export/events/{id}.json` (fixture-shaped,
  round-trips), plus CSVs for scores, results, projects and audit.
* Every seeded account uses password `dogfood` (override `SEED_PASSWORD`).

## What we would change with more time

* Move the rate limiter to the database (or Redis) for multi-node deployments.
* Email delivery for invites (currently the invite link is shown to the
  organizer).
* A proper migration runner (the table exists; only the base version is used).
