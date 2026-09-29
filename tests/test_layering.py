"""core/ must never import extensions/. This is what makes T4 deletable."""
import re
from pathlib import Path

CORE = Path(__file__).resolve().parents[1] / "src" / "rubrica" / "core"


def test_core_never_imports_extensions():
    offenders = []
    for py in CORE.rglob("*.py"):
        text = py.read_text()
        if re.search(r"^\s*(from|import)\s+[\w.]*extensions", text, re.M):
            offenders.append(str(py))
    assert not offenders, offenders


def test_app_boots_without_extensions(monkeypatch, tmp_path):
    import os
    monkeypatch.setenv("RUBRICA_EXTENSIONS", "0")
    from fastapi.testclient import TestClient
    from rubrica.app import create_app
    app = create_app(database_path=str(tmp_path / "noext.db"), quiet=True)
    with TestClient(app) as c:
        assert c.get("/projects").status_code == 200
        assert c.get("/api/v1/openapi.json").status_code == 404
        assert c.get("/api/judge/scores", headers={"Cookie": "session=jdg_a_91bc3e4f"}).status_code == 200
