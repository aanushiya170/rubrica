# Rubrica

> A self-hosted hackathon portal with backend-isolated judging, blind-anchor
> calibration, documented cross-judge normalization, replayable published
> results, community voting with anti-abuse controls, and a REST/OpenAPI,
> webhook, certificate and embed layer built as a detachable module.

**DOGFOOD 2026 entry.** Python 3.12 · FastAPI · SQLite · one container · MIT.

## Acceptance report

```
DOGFOOD 2026 acceptance report
portal: http://localhost:8080
claimed: T1 T2 T3 T4
fixtures: fixtures.json

T1  gallery is public ................. PASS
T1  project from fixtures shown ....... PASS
T1  closed event refuses submissions .. PASS
T2  judge sees own scores ............. PASS
T2  judge cannot see peer scores ...... PASS
T2  participant blocked ............... PASS
T2  csv export works .................. PASS

claimed T1 T2 T3 T4, verified T1 T2
note: claimed but not verified: T3 T4
```

`run.py` contains seven checks and all of them are T1/T2, so the last line is
what the checker prints for *any* portal that claims T3 or T4 — it cannot
verify those tiers, by design. What backs the T3/T4 claim here is
`tests/` (37 tests, `python3 -m pytest`) and the walkthrough below. Honest gaps
are listed under **Limitations**.

## Run it

```
docker compose up
```

That's it. The image builds from `python:3.12-slim`, seeds `fixtures.json`,
creates a live demo event, and prints the four test logins:

```
seeded. fixture evt_01: 41 projects, 30 judges, 8 tracks, 126 scores
demo event evt_02: created (submissions open)
test logins:
  organizer    Cookie: session=org_7f2a9c1d
  judge_a      Cookie: session=jdg_a_91bc3e4f
  judge_b      Cookie: session=jdg_b_44de5a6b
  participant  Cookie: session=prt_2e88c7d0
password for every seeded account: dogfood
```

Open http://localhost:8080. Then, in another terminal:

```
python3 run.py .dogfood.toml
```

Without Docker: `pip install -r requirements.txt && PYTHONPATH=src python3 -m rubrica`.
Tests: `pip install -r requirements-dev.txt && python3 -m pytest`.

The seed is idempotent — `docker compose up` twice still yields 41 fixture
projects. Data lives in `./data` (SQLite file + signing key). No network is
needed after the image is built.

### Sign in as

| who | email | what to look at |
|---|---|---|
| organizer | `organizer@rubrica.local` | Control room, integrity alerts, normalization, publish + verify, audit |
| a fixture judge | `diego.herrera@example.org` (jdg_24) | own queue, only own scores, pairwise mode, judge record |
| demo participant | `maker@rubrica.local` | team, draft + submitted project in the open demo event |
| checker participant | `participant@rubrica.local` | on tm_01 in the closed fixture event — every submit is refused |

Password for all: `dogfood`.

## What it does

**T1 Core.** Accounts and sessions; roles visitor / participant / judge /
organizer / admin; event creation with dates, tracks, prizes and rubric; team
formation by invite link (cap 4); project draft → edit → submit until the
event's `submissions_close`, enforced in the service layer from the event's own
dates; public gallery ordered by project id with search and track/event filter.

**T2 Judging.** Judge invitations; organizer-weighted rubric with immutable
versions; a blind-overlap assignment engine (track-eligible, never own team,
shared anchors for calibration); backend role isolation — `GET
/api/judge/scores?judge=jdg_24` as another judge is a 403 from the API, not a
hidden column; live progress dashboard (per judge, per project, who hasn't
started); documented cross-judge normalization with a stored calibration table;
CSV export; integrity alerts (duplicate, zero-variance, low-sample, coverage);
duplicate resolution that sets `canonical_project_id` and never deletes;
publish freezes inputs + SHA-256, **Verify** recomputes and diffs.

**T3 Public.** Authenticated community voting, one vote per project per user
(UNIQUE constraint), 5 votes per event, no self-votes; comments with organizer
hide; results and vote totals hidden from everyone but staff during the voting
window and until published; per-voter randomized ballots; sliding-window rate
limits per user and per IP; duplicate attempts audited; append-only, readable,
filterable, CSV-exportable audit trail.

**T4 Stretch** (`src/rubrica/extensions/`, detachable with `RUBRICA_EXTENSIONS=0`).
`/api/v1` REST API with scoped API keys, an OpenAPI 3.1 document and an
offline docs page; HMAC-signed outbound webhooks fed by the audit signal
stream, with delivery records; printable participant certificates; Ed25519-signed
judge participation records verifiable offline against a published public key;
embeddable gallery (iframe page, script tag, CORS JSON); bulk import/export of
whole events as fixture-shaped JSON (round-trips); pairwise judging with a
Bradley–Terry fit.

**Bonuses.** Normalization Proof → `JUDGING.md` (fixture-verified, tested).
Threat Model → `THREAT-MODEL.md`. API First → `/api/v1/docs`. Pairwise →
`extensions/pairwise.py`.

## Five-minute walkthrough (the demo script)

1. `docker compose up` — 41 projects, 30 judges, 8 tracks seeded.
2. Gallery → search "Dry Harbour" → two records from tm_07.
3. Sign in as organizer → Control room → **Sample Hack 2026**: alerts for the
   duplicate, jdg_07 (zero variance), jdg_01/jdg_23 (one review), coverage.
4. Judge A: `/judge` shows only her queue; score form; save.
5. **The money shot:**
   `curl -i -H 'Cookie: session=jdg_b_44de5a6b' localhost:8080/api/judge/scores?judge=jdg_24` → **403**.
6. Control room → Normalization → Run (k=3) → calibration table; raw rank vs
   normalized rank with movement arrows.
7. Duplicate → **Merge prj_41 into prj_07** → prj_41 excluded, scores kept,
   re-run drops it from ranking.
8. Publish → **Verify** → MATCH, 126 scores, 41 rows recomputed.
9. Demo event (evt_02): sign in as `maker@rubrica.local`, submit; as a new
   user, vote on the ballot; results page says *hidden* during voting.
10. `curl -H 'Cookie: session=org_7f2a9c1d' localhost:8080/api/export.csv | head`.
11. `/api/v1/docs`, `/embed/gallery?event=evt_01`, a judge record at
    `/certificates/evt_01/judge`.

## Routes the checker uses

| key | route | who | result |
|---|---|---|---|
| gallery | `GET /projects` | anyone | 200, fixture titles on page one |
| submit | `POST /projects/new` | participant (tm_01, evt_01 closed) | 403 |
| judge_scores | `GET /api/judge/scores` | judge_a (jdg_24) | 200 |
| peer_scores | `GET /api/judge/scores?judge=jdg_24` | judge_b (jdg_26) | 403 |
| judge_scores | same | participant | 403 |
| csv_export | `GET /api/export.csv` | organizer | 200 `text/csv` |

## Repo layout

```
.dogfood.toml  acceptance-report.txt  docker-compose.yml  Dockerfile  run.py  fixtures.json
README.md  ARCHITECTURE.md  DATA-MODEL.md  JUDGING.md  THREAT-MODEL.md  LICENSE
src/rubrica/core/         T1–T3: auth · events · teams · projects · judging · normalization · results · community · export
src/rubrica/extensions/   T4: api · webhooks · certificates · embeds · bulk · pairwise
tests/                    37 tests incl. the layering rule and the normalization proof
```

## Configuration

| variable | default | purpose |
|---|---|---|
| `DATABASE_PATH` | `data/rubrica.db` | SQLite file |
| `FIXTURES_PATH` | `./fixtures.json` | official fixture to seed into `evt_01` |
| `PORT` / `HOST` | `8080` / `0.0.0.0` | listener |
| `RUBRICA_EXTENSIONS` | `1` | `0` boots T1–T3 only |
| `SEED_PASSWORD` | `dogfood` | password for seeded accounts |
| `SEED_TOKEN_ORGANIZER` … `_PARTICIPANT` | fixed | the checker's session tokens; rotate for a real event |
| `SIGNING_KEY_PATH` | `data/signing_key.pem` | Ed25519 key for judge records |

## Limitations (honest ones)

* **No outbound email.** Invite links are shown to the organizer to copy;
  voter emails are not verified. Sybil resistance therefore rests on accounts,
  quotas, IP rate limits and the audit trail (see THREAT-MODEL.md).
* **Rate limiter is in-process.** Correct for one container; a multi-node
  deployment needs it moved to the database or Redis.
* **No per-form CSRF token** — cookies are SameSite=Lax and all mutations are
  non-GET, which blocks cross-site form posts, but a same-site XSS would not be
  contained by a token we don't have.
* **Normalization is a documented heuristic, not a Bayesian model.** `k=3` is a
  choice; small samples stay uncertain; review counts are shown everywhere so
  nobody mistakes a thin result for a solid one.
* **No hash-chained audit log.** Recomputation from recorded inputs is the
  claim we make and test; a chain in the same database would not extend the
  trust boundary. Export `audit.csv` off-box after publishing.
* **Webhooks retry zero times.** Failures are recorded with the error; there is
  a manual "test" endpoint but no backoff queue.
* **UI is server-rendered and plain.** It is complete for every flow, but it is
  not the differentiator and was not built to be.
* **Pairwise ranking is separate** from the rubric ranking; there is no blend.

## License

MIT — see `LICENSE`.
