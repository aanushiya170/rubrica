# DATA-MODEL.md

Source of truth: `src/rubrica/core/schema.sql` (core) plus the `SCHEMA` string
in each `extensions/*.py`. SQLite, foreign keys on, WAL journal. All ids are
opaque strings (`prj_…`, `jdg_…`); the fixture's own ids are used verbatim.
All timestamps are ISO-8601 UTC with a trailing `Z`.

## Core

```
users(id, email UNIQUE, name, role ∈ {participant,judge,organizer,admin}, password_hash, created_at)
sessions(token PK, user_id → users, created_at, expires_at)

events(id, name, description, submissions_open, submissions_close, voting_open, voting_close,
       is_historical, results_published, created_by → users, created_at)
tracks(id, event_id → events, name)
prizes(id, event_id → events, rank, title, amount)

teams(id, event_id → events, name, invite_code UNIQUE, created_at)
team_members(team_id → teams, user_id → users)                       PK(team_id, user_id)

projects(id, event_id → events, team_id → teams, track_id → tracks?, title, summary, repo_url,
         demo_url, thumbnail_url, status ∈ {draft,submitted,excluded}, submitted_at,
         canonical_project_id → projects?, created_at, updated_at)

rubrics(id, event_id → events, version, active, created_at)         UNIQUE(event_id, version)
criteria(id, rubric_id → rubrics, key, name, weight ∈ [0,1], position) UNIQUE(rubric_id, key)

judge_tracks(judge_id → users, track_id → tracks)                    PK(judge_id, track_id)
judge_invites(id, event_id, email, token UNIQUE, track_ids JSON, accepted_by → users?, created_at)

assignments(id, event_id, project_id → projects, judge_id → users, is_anchor, created_at)
                                                                     UNIQUE(project_id, judge_id)
scores(id, assignment_id → assignments UNIQUE, event_id, judge_id, project_id, rubric_version,
       criteria_json, weighted_raw, comment, submitted_at)

normalization_runs(id, event_id, method, params_json, stats_json, created_by, created_at)
normalization_results(run_id → runs, project_id, review_count, raw_avg, normalized,
                      rank_raw, rank_norm)                           PK(run_id, project_id)

result_snapshots(id, event_id, normalization_run_id → runs, rubric_version,
                 inputs_json {score_ids, project_ids, params, method, excluded},
                 inputs_sha256, results_json, published_by, published_at)

audit_events(seq AUTOINCREMENT, id UNIQUE, timestamp, actor_id, action, entity_type, entity_id, payload_json)
votes(id, event_id, project_id → projects, voter_id → users, created_at)  UNIQUE(project_id, voter_id)
comments(id, project_id → projects, user_id → users, body, created_at, hidden)
schema_migrations(version PK, applied_at)
```

### Decisions worth defending

* **Judges are users.** A fixture judge `jdg_07` is a `users` row with
  `id='jdg_07'` and `role='judge'`; `judge_tracks` gives eligibility. This keeps
  `assignments.judge_id` and `scores.judge_id` pointing at the same identity the
  session resolves to, which is what the scope check compares against.
* **`scores` has exactly one row per assignment** (`assignment_id UNIQUE`).
  Re-scoring updates in place and writes an audit event; there is no
  "latest score" ambiguity.
* **`weighted_raw` is denormalised** into `scores` next to `criteria_json` and
  `rubric_version`. Recomputing it is trivial, but storing it makes exports and
  normalization independent of later rubric edits.
* **`canonical_project_id`** lets an organizer say "prj_41 is the same as
  prj_07" without deleting either record. `status='excluded'` removes it from
  ranking and ballots; its scores stay as evidence.
* **`is_historical`** on events is a hard "read-only for submissions" switch,
  used for the official fixture so no clock skew can ever reopen it.
* **`audit_events.seq`** is a rowid so the log has a total order independent
  of timestamp resolution. Nothing in the codebase updates or deletes from
  this table.
* **`result_snapshots.inputs_json`** stores *ids*, not copies, so verification
  proves the current rows still produce the published table — which is the
  actual question an organizer gets asked.

## Extensions

```
api_keys(id, user_id → users, key_hash UNIQUE, prefix, label, scopes, created_at, last_used, revoked)
webhooks(id, event_id → events?, url, event_types JSON, secret, active, created_by, created_at)
webhook_deliveries(id, webhook_id → webhooks, event_type, payload_json, status, response_code,
                   error, created_at, delivered_at)
certificates(id, event_id, user_id, kind ∈ {participant,judge}, record_json, signature, algorithm, issued_at)
                                                                     UNIQUE(event_id, user_id, kind)
pairwise_comparisons(id, event_id, judge_id, project_a, project_b, winner, created_at)
```

## Import paths (in)

| what | how |
|---|---|
| Official fixture | loaded on boot from `FIXTURES_PATH` (default `./fixtures.json`) into `evt_01`; idempotent |
| Any event | `POST /api/v1/import/events` with a fixture-shaped document (organizer). Optional keys: `prizes`, `rubric`, `event.description/voting_*`, `historical`. Same loader as the fixture; re-import of an existing event id → 409 |
| Judges | organizer invite link (`/judge/invite/<token>`) or included in an imported document |
| Teams | invite link `/join/<code>` |

## Export paths (out)

| what | route | format |
|---|---|---|
| all scores (checker route) | `GET /api/export.csv[?event=]` | CSV: one row per review, one column per criterion |
| normalized results | `GET /api/export/results.csv?event=` | CSV |
| projects | `GET /api/export/projects.csv[?event=]` | CSV |
| audit trail | `GET /api/export/audit.csv` | CSV |
| whole event | `GET /api/v1/export/events/{id}.json` | fixture-shaped JSON; round-trips through import |
| the database | `./data/rubrica.db` | a single SQLite file |

All export routes require organizer/admin.

## Fixture → schema mapping

| fixtures.json | table(s) |
|---|---|
| `event` | `events` (`is_historical=1`, `submissions_close` verbatim) |
| `tracks[]` | `tracks` |
| `judges[]` | `users(role=judge)` + `judge_tracks` |
| `teams[]` | `teams` + `users(role=participant)` per member email + `team_members` |
| `projects[]` | `projects(status=submitted)` |
| `scores[]` | one `assignments(is_anchor=1)` + one `scores` row each; `weighted_raw` computed with the 40/35/25 rubric (`rubric_version=1`) |
