"""The normalization proof: recompute the JUDGING.md table from fixtures.json."""
import json
from pathlib import Path

import pytest

from rubrica.core.services.normalization import normalize

FX = json.loads((Path(__file__).resolve().parents[1] / "fixtures.json").read_text())
W = {"functionality": 0.40, "quality": 0.35, "innovation": 0.25}


def rows():
    return [{"id": f"s{i}", "judge_id": s["judge"], "project_id": s["project"],
             "weighted_raw": sum(s["criteria"][k] * w for k, w in W.items())} for i, s in enumerate(FX["scores"])]


def test_global_stats_match_documented_values():
    n = normalize(rows())
    assert n.global_mean == pytest.approx(3.568254, abs=1e-6)
    assert n.global_sd == pytest.approx(0.670661, abs=1e-6)


def test_judge_table_matches_documented_values():
    n = normalize(rows(), k=3)
    j = {x.judge_id: x for x in n.judges}
    assert j["jdg_07"].n == 3 and j["jdg_07"].zero_variance and j["jdg_07"].shrunk_mean == pytest.approx(3.784, abs=5e-4)
    assert j["jdg_01"].n == 1 and j["jdg_01"].low_sample and j["jdg_01"].shrunk_mean == pytest.approx(3.176, abs=5e-4)
    assert j["jdg_23"].n == 1 and j["jdg_23"].shrunk_mean == pytest.approx(3.464, abs=5e-4)


def test_zero_variance_policy():
    base = normalize(rows(), zero_variance_policy="baseline")
    shrink = normalize(rows(), zero_variance_policy="shrink")
    b = {s.score_id: s.normalized for s in base.scores if s.judge_id == "jdg_07"}
    s = {s.score_id: s.normalized for s in shrink.scores if s.judge_id == "jdg_07"}
    assert all(v == pytest.approx(base.global_mean) for v in b.values())
    assert len(set(s.values())) == 1  # identical raw → identical normalized, no fake ordering either way
    assert list(s.values())[0] > base.global_mean  # shrink keeps "above average" information


def test_shrinkage_pulls_small_samples_toward_global():
    n = normalize(rows(), k=3)
    j = {x.judge_id: x for x in n.judges}
    assert abs(j["jdg_01"].shrunk_mean - n.global_mean) < abs(j["jdg_01"].raw_mean - n.global_mean)
    big = normalize(rows(), k=1000)
    assert all(abs(x.shrunk_mean - big.global_mean) < 0.02 for x in big.judges)


def test_ranks_and_bounds():
    n = normalize(rows())
    assert sorted(p.rank_norm for p in n.projects) == list(range(1, 42))
    assert all(1.0 <= s.normalized <= 5.0 for s in n.scores)
    assert {p.review_count for p in n.projects} == {2, 3, 4, 5}


def test_excluded_projects_are_dropped():
    n = normalize(rows(), excluded_project_ids={"prj_41"})
    assert len(n.projects) == 40 and n.excluded_project_ids == ["prj_41"]


def test_empty_is_safe():
    assert normalize([]).projects == []
