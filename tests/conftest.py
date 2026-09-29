import os
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
os.environ["FIXTURES_PATH"] = str(ROOT / "fixtures.json")

from rubrica.app import create_app  # noqa: E402
from rubrica.core import seed, signals  # noqa: E402

ORG = {"Cookie": "session=" + seed.TEST_LOGINS["organizer"][1]}
JUDGE_A = {"Cookie": "session=" + seed.TEST_LOGINS["judge_a"][1]}
JUDGE_B = {"Cookie": "session=" + seed.TEST_LOGINS["judge_b"][1]}
PARTICIPANT = {"Cookie": "session=" + seed.TEST_LOGINS["participant"][1]}


@pytest.fixture(scope="session")
def client(tmp_path_factory):
    d = tmp_path_factory.mktemp("db")
    os.environ["SIGNING_KEY_PATH"] = str(d / "key.pem")
    signals.clear()
    app = create_app(database_path=str(d / "test.db"), quiet=True)
    with TestClient(app, follow_redirects=False) as c:
        yield c


@pytest.fixture
def db(client):
    return client.app.state.db


def login(client, email, password="dogfood"):
    r = client.post("/login", data={"email": email, "password": password, "next": "/"})
    assert r.status_code == 303, r.text
    tok = r.cookies["session"]
    client.cookies.clear()  # keep the shared client anonymous; tests pass headers explicitly
    return {"Cookie": "session=" + tok}


def register(client, email, name="Tester"):
    r = client.post("/register", data={"email": email, "name": name, "password": "secret1", "next": "/"})
    assert r.status_code == 303, r.text
    tok = r.cookies["session"]
    client.cookies.clear()
    return {"Cookie": "session=" + tok}
