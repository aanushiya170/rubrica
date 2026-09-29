# THREAT-MODEL.md — what we stopped, what we didn't

Scope: a self-hosted hackathon portal run by one organizer team, reachable on
the public internet during an event. Assets, in order of value: the integrity
of judge scores and the published ranking; the community vote; participant
data; availability during the submission deadline.

Format per threat: **attack → control → residual risk**.

## Judging integrity

**IDOR / peer-score disclosure.** Judge B requests judge A's scores by URL.
→ Identity is read from the session only; `?judge=` is a request the scope
check may refuse (`auth.assert_judge_scope`). Enforced in the service layer,
so HTML, `/api`, and `/api/v1` all give 403. Tested by `run.py` and
`tests/test_acceptance_t1_t2.py`.
→ Residual: organizers and admins can see everything by design.

**Scoring a project you were not assigned.** → `assert_judge_scope(project_id)`
checks the assignments table before any score write. Judges are never assigned
their own team's projects.

**Score tampering after the fact.** A privileged insider edits a row.
→ Every write is audited (append-only, monotonic `seq`). Published snapshots
freeze input ids + parameters + SHA-256; `verify` recomputes and diffs, and a
test proves it detects a changed score.
→ Residual: someone with database-file access can also rewrite the audit table
and the snapshot. We chose recomputation over a hash chain because a chain
stored in the same database has the same trust boundary; the honest mitigation
is to export `audit.csv` and the snapshot JSON off-box after publishing.

**Rubric edits rewriting history.** → Rubrics are versioned; scores carry their
version and are never recalculated silently.

**Judge collusion (agreeing to inflate a friend's project).** → Blind overlap
means a colluding judge does not know who else reviews the project or what
they gave. Normalization shrinks outliers toward the judge's own calibrated
mean. Zero-variance and low-sample judges are flagged for human review.
→ Residual: coordinated, consistent inflation across several judges is not
detectable statistically from scores alone; the organizer's calibration table
and per-judge raw means are the tool for that conversation.

**Judge account takeover.** → scrypt password hashes, HttpOnly SameSite=Lax
session cookies, failed logins audited.
→ Residual: no MFA, no lockout, no password-reset email (no mail server is
allowed by the offline rule). Deploy behind TLS.

## Community vote

**Sybil accounts.** One person registers many emails and votes many times.
→ Voting requires an account; per-user vote quota per event (5); per-IP rate
limit; every vote audited with the client address.
→ Residual: email is not verified (no outbound mail offline), so a determined
attacker can still create accounts. Organizers can (a) treat the community
vote as advisory — it is a separate table from the rubric ranking — and (b)
read `vote.*` audit rows grouped by client address. Email verification and
invite-only voting are the next controls to add when an SMTP relay exists.

**Ballot stuffing / duplicate votes.** → UNIQUE(project, voter) in the schema,
not just in code; the rejected duplicate is itself audited
(`vote.duplicate_rejected`), so an attempted stuffing campaign leaves a trace.

**Self-voting.** → Team members cannot vote for their own project (403).

**Position bias.** → Ballot order is a per-voter deterministic shuffle.

**Bandwagon / herding.** → Vote totals and rubric results are hidden from
everyone but staff while the voting window is open.

**Comment spam / abuse.** → Rate limit (5/min), length cap, organizer hide
(audited). Residual: no content moderation beyond that.

## Submissions

**Late submission via the API when the UI is closed.** → Deadline is enforced
in `projects.create_project/_writable` from the event's own dates, with an
additional `is_historical` hard stop; the checker's probe returns 403.

**Editing someone else's project.** → Team membership check on every write.

**Duplicate submissions gaming the ranking.** → Integrity alert for same team +
same repo/title; organizer merge marks `canonical_project_id` and excludes the
duplicate from ranking without deleting evidence.

**Invite-link leakage.** → Invite codes are random; teams cap at 4; joining is
audited. Residual: a leaked link can be used by anyone until the team is full;
regeneration is not implemented.

## Platform

**CSRF.** → Session cookie is `SameSite=Lax`; all state changes are POST/PUT/
DELETE. Residual: no per-form CSRF token; top-level cross-site POST from an
attacker page is blocked by Lax, but a same-site XSS would not be. Templates
auto-escape.

**API key leakage.** → Keys are stored hashed, shown once, prefixed for
identification, scoped (`read` vs `read write`), revocable.

**Webhook forgery at the receiver.** → Each delivery is HMAC-SHA256 signed with
a per-webhook secret. Residual: no replay protection beyond the delivery id in
the header; receivers should de-duplicate on `X-Rubrica-Delivery`.

**Forged judge records / certificates.** → Ed25519 signatures over canonical
JSON; public key served at `/api/v1/records/public-key`; anyone can verify
offline. Residual: the private key lives in `./data`; protect that volume.

**Denial of service around the deadline.** → SQLite with WAL and a single
process handles hackathon-scale traffic comfortably; there is no global
lock on reads. Residual: no request-level rate limit on unauthenticated
routes; put a reverse proxy in front for a public event.

**Secrets in the repo.** → None. The four checker sessions are fixed *test*
tokens, printed on boot, and overridable with `SEED_TOKEN_*`. Rotate them (or
delete the seeded sessions) before running a real event.

## Explicitly not addressed

Email verification, MFA, password reset, per-form CSRF tokens, content
moderation, multi-node rate limiting, and hardware-backed key storage. Each is
listed so the gap is a decision, not a surprise.
