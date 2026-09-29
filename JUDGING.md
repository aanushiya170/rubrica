# JUDGING.md — assignment, scoring, normalization, and what we don't claim

This document is the Normalization Proof. Every number below is recomputed by
`tests/test_normalization.py` from `fixtures.json`; if the code and this file
ever disagree, the test fails.

## 1. Rubric and raw score

An organizer configures criteria with weights that must sum to 1.0 (server
rejects anything else). A judge scores each criterion 1–5. The stored raw score
is

    S_jp = Σ_c  w_c · score_c(j, p)

Every stored score carries the `rubric_version` it was produced under. Editing a
rubric creates a new version; historical scores are never rewritten.

The fixture rubric used throughout: **Functionality 40 % · Quality 35 % ·
Innovation 25 %**.

## 2. Assignment strategy — blind overlap, not disjoint batches

Fully disjoint batches (each judge sees a private set of projects) make
cross-judge calibration statistically impossible: there is no shared reference
to distinguish a strict judge from a weak project. The assignment engine
(`core/services/judging.py::assign`) therefore builds **blind overlap**:

* A judge is eligible only for projects in the tracks they cover (a project with
  no track is open to every judge of the event).
* A judge never receives their own team's project.
* Per track, `anchor_count` projects are **anchors**: every eligible judge
  reviews them independently. Anchors give the calibration a shared reference.
* Remaining projects are dealt to the least-loaded eligible judges until each
  has `reviews_per_project` reviewers.
* **A judge never sees another judge's numbers, anchor or not.** The overlap is
  in *what* is reviewed, never in *who sees whose scores*. `GET
  /api/judge/scores?judge=<someone else>` returns 403 from the backend.

The engine is idempotent (existing (project, judge) pairs are kept) and seeded
(`seed=42`) so an organizer can regenerate and get the same plan.

In the official fixture every project already has 2–5 independent reviews, so
every fixture review is treated as an anchor observation.

## 3. Cross-judge normalization

### 3.1 What it is, honestly named

We call the method a **sample-size-shrunk location-scale normalization
heuristic, inspired by empirical-Bayes reasoning**. It is *not* a full Bayesian
posterior: we linearly shrink a judge's mean and variance toward the global
values with a fixed prior strength `k`, then z-score and map back onto the
global scale. Someone with a statistics background would notice if we called
this "empirical Bayes" outright, so we don't.

### 3.2 Definitions

For an event with all raw scores `S`:

    μ_g = mean(S)                     global mean
    v_g = population variance(S)      σ_g = sqrt(v_g)

For judge `j` with `n_j` scores:

    μ_j = mean of j's scores
    v_j = population variance of j's scores   (divide by n_j, not n_j − 1)

Shrinkage with prior strength `k` (default 3):

    μ_j* = (n_j·μ_j + k·μ_g) / (n_j + k)
    σ_j* = sqrt( (n_j·v_j + k·v_g) / (n_j + k) )

Normalized score of judge `j` on project `p`:

    z    = (S_jp − μ_j*) / σ_j*
    N_jp = clip( μ_g + z·σ_g , 1 , 5 )

Project result = mean of its `N_jp`; ranking by that mean, ties broken by
project id. The raw ranking (mean of `S_jp`) is always shown alongside.

Why population variance: with `n_j = 1` the sample variance is undefined and
with `n_j = 2` it is wildly unstable; the `k·v_g` term already supplies the
prior mass, and using `/n` keeps `σ_j*` finite and monotone in `n_j`.

### 3.3 Fixture values (recomputed, not asserted)

Weighted raw scores for all 126 fixture reviews:

| statistic | value |
|---|---:|
| global mean `μ_g` | **3.568254** |
| global population sd `σ_g` | **0.670661** |
| global variance `v_g` | 0.449786 |

Judge table with `k = 3` (selected rows; the full 30-row table is on the
organizer's control room and in `GET /api/v1/events/evt_01/normalization`):

| judge | n | raw mean | raw sd | shrunk mean μ_j* | shrunk sd σ_j* | flag |
|---|---:|---:|---:|---:|---:|---|
| jdg_07 | 3 | 4.000 | 0.000 | **3.784** | 0.474 | zero variance |
| jdg_01 | 1 | 2.000 | — | **3.176** | 0.581 | low sample |
| jdg_23 | 1 | 3.150 | — | **3.464** | 0.581 | low sample |
| jdg_24 | 11 | 3.318 | 0.645 | 3.372 | 0.651 | |
| jdg_26 | 10 | 3.705 | 0.522 | 3.673 | 0.560 | |
| jdg_29 | 9 | 3.489 | 0.412 | 3.509 | 0.489 | |

Worked example, jdg_01's single score of 2.00:

    μ_j* = (1·2.000 + 3·3.568254) / 4 = 3.176190
    σ_j* = sqrt((1·0 + 3·0.449786) / 4) = 0.580809
    z    = (2.000 − 3.176190) / 0.580809 = −2.0251
    N    = 3.568254 + (−2.0251)(0.670661) = 2.210

A single harsh score is softened from 2.00 to 2.21 — still clearly low, but no
longer allowed to sink a project on one judge's word. With eleven reviews,
jdg_24's shrunk mean (3.372) stays close to their raw mean (3.318): the data
wins when there is enough of it.

Largest rank movements (raw → normalized) in the fixture:

| project | reviews | raw avg | normalized | raw rank | norm rank |
|---|---:|---:|---:|---:|---:|
| prj_19 | 2 | 3.575 | 3.322 | 17 | 30 |
| prj_28 | 3 | 3.400 | 3.108 | 28 | 37 |
| prj_27 | 3 | 3.383 | 3.507 | 29 | 21 |
| prj_12 | 3 | 3.450 | 3.553 | 23 | 16 |

prj_19 is instructive: two reviews, one of them from zero-variance jdg_07,
whose "4.0" carries no relative information (see 3.4) — so the project's
normalized standing rests on its other single review.

The top of the table is stable (prj_34 and prj_11 are 1st and 2nd both ways),
which is what you want from a calibration step: it should move the borderline,
not overturn the obvious.

### 3.4 Zero-variance rule

jdg_07 gave 4/4/4 to all three of their projects. A judge who scores every
project identically provides **no relative separation** between them. Two
policies are implemented (`zero_variance_policy`):

* `baseline` (default): each of their scores maps to `μ_g`. Their contribution
  becomes "no information" rather than "everything is 4.0", which would
  otherwise push all three projects up by the same amount.
* `shrink`: fall through to the formula. `σ_j*` is still positive because of
  the `k·v_g` term, and all three scores map to the same value (3.874). No fake
  ordering is manufactured either way; the difference is whether "4.0 across
  the board" is read as a mild positive signal or as no signal.

We default to `baseline` and say so in the UI. The alternative is one
parameter away and both are stored on the normalization run.

### 3.5 Parameters and limitations — stated up front

* **`k = 3` is a chosen parameter, not a universal optimum.** It says "a
  judge's own data starts to dominate once they have more than three reviews".
  The control room lets an organizer re-run with another `k`; each run is
  stored with its parameters and the published snapshot records which one was
  used.
* **Very small samples stay uncertain after shrinkage.** Shrinking jdg_01
  toward the mean does not make one review as trustworthy as five. The results
  table shows review count next to every project and flags anything under 3 so
  nobody mistakes a thin result for a solid one.
* **Location-scale only.** We correct a judge's average level and spread. We
  do not model judge × track interactions, criterion-specific harshness, or
  time-of-day drift.
* **Clipping to [1, 5]** can compress extreme normalized values; in the fixture
  nothing clips.
* **Overlap is what makes this meaningful.** With no anchors the estimates of
  `μ_j` and `μ_g` are confounded with project quality. That is why the
  assignment engine builds overlap on purpose (§2).

### 3.6 Reproducibility

Every normalization run stores `{method, params, global stats, per-judge
stats, per-project results}`. Publishing freezes the exact `score_ids`,
`project_ids`, exclusions and parameters plus a SHA-256 of those inputs. **Verify**
(`GET /api/results/{snapshot}/verify`) recomputes from the recorded inputs and
diffs every published row. `tests/test_core_flows.py` tampers with a score and
asserts the verification fails.

## 4. Integrity alerts the fixture triggers

| alert | what the fixture contains | what the portal does |
|---|---|---|
| Duplicate submission | prj_07 and prj_41 "Dry Harbour", same team tm_07, same repo, submitted 04:29 and 17:57 UTC | Keep / Merge / Exclude buttons; merge sets `canonical_project_id`, never deletes; excluded projects drop out of ranking, their scores stay as evidence |
| Zero-variance judge | jdg_07, three identical 4/4/4 reviews | flagged; `baseline` policy in normalization |
| Low-sample judges | jdg_01 and jdg_23, one review each | flagged; heavily shrunk |
| Uneven coverage | 2–5 reviews per project | review count shown everywhere a score is |
| Empty comments | 51 of 126 comments are empty | omitted from views rather than rendered as blank boxes |

## 5. Pairwise mode (bonus) — Bradley–Terry

`extensions/pairwise.py` lets a judge pick the stronger of two assigned
projects. Strengths are fit with the MM algorithm (Hunter 2004):

    P(i beats j) = π_i / (π_i + π_j)
    π_i ← W_i / Σ_{j≠i} n_ij / (π_i + π_j)

with 0.5 symmetric pseudo-wins per pair so every project has a finite estimate
under sparse data, and strengths normalised to geometric mean 1. Wins and
comparison counts are shown next to each strength for the same reason review
counts are shown next to normalized scores. This is a separate ranking; it is
not blended into the rubric result.
