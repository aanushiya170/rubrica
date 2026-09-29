"""Fixture/event integrity alerts for the organizer control room."""
from __future__ import annotations

from ..db import Database
from . import normalization as norm_svc


def alerts(db: Database, event_id: str) -> list[dict]:
    out: list[dict] = []

    dupes = db.all(
        "SELECT a.id AS a_id, b.id AS b_id, a.title, a.team_id, a.repo_url, a.submitted_at AS a_at,"
        " b.submitted_at AS b_at, b.status AS b_status, b.canonical_project_id"
        " FROM projects a JOIN projects b ON a.event_id = b.event_id AND a.id < b.id"
        " AND a.team_id = b.team_id AND (a.repo_url = b.repo_url OR lower(a.title) = lower(b.title))"
        " WHERE a.event_id = ? AND a.status != 'draft' AND b.status != 'draft'", (event_id,))
    for d in dupes:
        resolved = d["b_status"] == "excluded" and d["canonical_project_id"] == d["a_id"]
        out.append({"kind": "duplicate", "severity": "warn" if not resolved else "info",
                    "title": f"Duplicate submission — \"{d['title']}\"",
                    "detail": f"{d['a_id']} ↔ {d['b_id']}, same team ({d['team_id']}), same repo_url, "
                              f"submitted {d['a_at']} and {d['b_at']}.",
                    "project_id": d["b_id"], "canonical_id": d["a_id"], "resolved": resolved})

    rows = norm_svc.score_rows_for_event(db, event_id)
    if rows:
        n = norm_svc.normalize(rows, excluded_project_ids=norm_svc.excluded_projects(db, event_id))
        for j in n.judges:
            if j.zero_variance:
                out.append({"kind": "zero_variance", "severity": "warn",
                            "title": f"Zero-variance judge — {j.judge_id}",
                            "detail": f"{j.n} reviews, all identical ({j.raw_mean:.2f}). Their scores map to the "
                                      f"event baseline in normalization.", "judge_id": j.judge_id})
        low = [j for j in n.judges if j.low_sample]
        if low:
            out.append({"kind": "low_sample", "severity": "info",
                        "title": "Low-sample judges — " + ", ".join(j.judge_id for j in low),
                        "detail": f"{len(low)} judge(s) with a single review; heavily shrunk toward the global mean."})
        counts = [p.review_count for p in n.projects]
        if counts and max(counts) - min(counts) >= 2:
            out.append({"kind": "coverage", "severity": "info", "title": "Uneven review coverage",
                        "detail": f"projects range from {min(counts)} to {max(counts)} reviews; "
                                  f"the results table shows review count next to every score."})
    unfinished = db.all(
        "SELECT a.judge_id, COUNT(*) AS n FROM assignments a LEFT JOIN scores s ON s.assignment_id = a.id"
        " WHERE a.event_id = ? AND s.id IS NULL GROUP BY a.judge_id", (event_id,))
    if unfinished:
        out.append({"kind": "unfinished", "severity": "info",
                    "title": f"Unfinished batches — {len(unfinished)} judge(s)",
                    "detail": ", ".join(f"{u['judge_id']} ({u['n']} left)" for u in unfinished)})
    return out
