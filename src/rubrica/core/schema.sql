-- Rubrica core schema (SQLite). Applied idempotently on boot.
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS schema_migrations (
  version INTEGER PRIMARY KEY,
  applied_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS users (
  id            TEXT PRIMARY KEY,
  email         TEXT NOT NULL UNIQUE COLLATE NOCASE,
  name          TEXT NOT NULL,
  role          TEXT NOT NULL CHECK (role IN ('participant','judge','organizer','admin')),
  password_hash TEXT NOT NULL,
  created_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sessions (
  token      TEXT PRIMARY KEY,
  user_id    TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  created_at TEXT NOT NULL,
  expires_at TEXT
);

CREATE TABLE IF NOT EXISTS events (
  id                TEXT PRIMARY KEY,
  name              TEXT NOT NULL,
  description       TEXT NOT NULL DEFAULT '',
  submissions_open  TEXT,
  submissions_close TEXT NOT NULL,
  voting_open       TEXT,
  voting_close      TEXT,
  is_historical     INTEGER NOT NULL DEFAULT 0,
  results_published INTEGER NOT NULL DEFAULT 0,
  created_by        TEXT REFERENCES users(id),
  created_at        TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS tracks (
  id       TEXT PRIMARY KEY,
  event_id TEXT NOT NULL REFERENCES events(id) ON DELETE CASCADE,
  name     TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS prizes (
  id       TEXT PRIMARY KEY,
  event_id TEXT NOT NULL REFERENCES events(id) ON DELETE CASCADE,
  rank     INTEGER NOT NULL,
  title    TEXT NOT NULL,
  amount   TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS teams (
  id          TEXT PRIMARY KEY,
  event_id    TEXT NOT NULL REFERENCES events(id) ON DELETE CASCADE,
  name        TEXT NOT NULL,
  invite_code TEXT NOT NULL UNIQUE,
  created_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS team_members (
  team_id TEXT NOT NULL REFERENCES teams(id) ON DELETE CASCADE,
  user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  PRIMARY KEY (team_id, user_id)
);

CREATE TABLE IF NOT EXISTS projects (
  id                   TEXT PRIMARY KEY,
  event_id             TEXT NOT NULL REFERENCES events(id) ON DELETE CASCADE,
  team_id              TEXT NOT NULL REFERENCES teams(id),
  track_id             TEXT REFERENCES tracks(id),
  title                TEXT NOT NULL,
  summary              TEXT NOT NULL DEFAULT '',
  repo_url             TEXT NOT NULL DEFAULT '',
  demo_url             TEXT NOT NULL DEFAULT '',
  thumbnail_url        TEXT NOT NULL DEFAULT '',
  status               TEXT NOT NULL CHECK (status IN ('draft','submitted','excluded')) DEFAULT 'draft',
  submitted_at         TEXT,
  canonical_project_id TEXT REFERENCES projects(id),
  created_at           TEXT NOT NULL,
  updated_at           TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_projects_event ON projects(event_id, status);

CREATE TABLE IF NOT EXISTS rubrics (
  id       TEXT PRIMARY KEY,
  event_id TEXT NOT NULL REFERENCES events(id) ON DELETE CASCADE,
  version  INTEGER NOT NULL,
  active   INTEGER NOT NULL DEFAULT 1,
  created_at TEXT NOT NULL,
  UNIQUE (event_id, version)
);

CREATE TABLE IF NOT EXISTS criteria (
  id        TEXT PRIMARY KEY,
  rubric_id TEXT NOT NULL REFERENCES rubrics(id) ON DELETE CASCADE,
  key       TEXT NOT NULL,
  name      TEXT NOT NULL,
  weight    REAL NOT NULL CHECK (weight >= 0 AND weight <= 1),
  position  INTEGER NOT NULL DEFAULT 0,
  UNIQUE (rubric_id, key)
);

CREATE TABLE IF NOT EXISTS judge_tracks (
  judge_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  track_id TEXT NOT NULL REFERENCES tracks(id) ON DELETE CASCADE,
  PRIMARY KEY (judge_id, track_id)
);

CREATE TABLE IF NOT EXISTS judge_invites (
  id         TEXT PRIMARY KEY,
  event_id   TEXT NOT NULL REFERENCES events(id) ON DELETE CASCADE,
  email      TEXT NOT NULL,
  token      TEXT NOT NULL UNIQUE,
  track_ids  TEXT NOT NULL DEFAULT '[]',
  accepted_by TEXT REFERENCES users(id),
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS assignments (
  id         TEXT PRIMARY KEY,
  event_id   TEXT NOT NULL REFERENCES events(id) ON DELETE CASCADE,
  project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  judge_id   TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  is_anchor  INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL,
  UNIQUE (project_id, judge_id)
);
CREATE INDEX IF NOT EXISTS idx_assignments_judge ON assignments(judge_id);

CREATE TABLE IF NOT EXISTS scores (
  id             TEXT PRIMARY KEY,
  assignment_id  TEXT NOT NULL UNIQUE REFERENCES assignments(id) ON DELETE CASCADE,
  event_id       TEXT NOT NULL REFERENCES events(id) ON DELETE CASCADE,
  judge_id       TEXT NOT NULL REFERENCES users(id),
  project_id     TEXT NOT NULL REFERENCES projects(id),
  rubric_version INTEGER NOT NULL,
  criteria_json  TEXT NOT NULL,
  weighted_raw   REAL NOT NULL,
  comment        TEXT NOT NULL DEFAULT '',
  submitted_at   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_scores_event ON scores(event_id);

CREATE TABLE IF NOT EXISTS normalization_runs (
  id          TEXT PRIMARY KEY,
  event_id    TEXT NOT NULL REFERENCES events(id) ON DELETE CASCADE,
  method      TEXT NOT NULL,
  params_json TEXT NOT NULL,
  stats_json  TEXT NOT NULL,
  created_by  TEXT REFERENCES users(id),
  created_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS normalization_results (
  run_id      TEXT NOT NULL REFERENCES normalization_runs(id) ON DELETE CASCADE,
  project_id  TEXT NOT NULL REFERENCES projects(id),
  review_count INTEGER NOT NULL,
  raw_avg     REAL NOT NULL,
  normalized  REAL NOT NULL,
  rank_raw    INTEGER NOT NULL,
  rank_norm   INTEGER NOT NULL,
  PRIMARY KEY (run_id, project_id)
);

CREATE TABLE IF NOT EXISTS result_snapshots (
  id                   TEXT PRIMARY KEY,
  event_id             TEXT NOT NULL REFERENCES events(id) ON DELETE CASCADE,
  normalization_run_id TEXT NOT NULL REFERENCES normalization_runs(id),
  rubric_version       INTEGER NOT NULL,
  inputs_json          TEXT NOT NULL,   -- {score_ids, project_ids, params}
  inputs_sha256        TEXT NOT NULL,
  results_json         TEXT NOT NULL,
  published_by         TEXT REFERENCES users(id),
  published_at         TEXT NOT NULL
);

-- T3 ---------------------------------------------------------------
CREATE TABLE IF NOT EXISTS audit_events (
  seq         INTEGER PRIMARY KEY AUTOINCREMENT,
  id          TEXT NOT NULL UNIQUE,
  timestamp   TEXT NOT NULL,
  actor_id    TEXT,
  action      TEXT NOT NULL,
  entity_type TEXT NOT NULL,
  entity_id   TEXT,
  payload_json TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS votes (
  id         TEXT PRIMARY KEY,
  event_id   TEXT NOT NULL REFERENCES events(id) ON DELETE CASCADE,
  project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  voter_id   TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  created_at TEXT NOT NULL,
  UNIQUE (project_id, voter_id)
);

CREATE TABLE IF NOT EXISTS comments (
  id         TEXT PRIMARY KEY,
  project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  user_id    TEXT NOT NULL REFERENCES users(id),
  body       TEXT NOT NULL,
  created_at TEXT NOT NULL,
  hidden     INTEGER NOT NULL DEFAULT 0
);
