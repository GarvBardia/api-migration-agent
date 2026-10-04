"""Public-demo run cap: 429 when 3 or more runs are pending or running."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from app.api.main import app

STEP6_REPO = Path(__file__).parent / "fixtures" / "step6_sample_repo"
client = TestClient(app)
BODY = {
    "repo_url": str(STEP6_REPO),
    "api_name": "run-cap-test",
    "version_from": "1.x",
    "version_to": "2.0",
}


def _session():
    from db import SessionLocal

    return SessionLocal()


@pytest.fixture
def active_runs():
    """Create N running runs; always clean up."""
    from db.models import MigrationRun

    ids: list = []

    def make(n: int, status: str = "running"):
        s = _session()
        try:
            for _ in range(n):
                r = MigrationRun(
                    repo_url=str(STEP6_REPO),
                    api_name="run-cap-test",
                    version_from="1.x",
                    version_to="2.0",
                    status=status,
                )
                s.add(r)
                s.flush()
                ids.append(r.id)
            s.commit()
        finally:
            s.close()

    yield make
    s = _session()
    try:
        s.query(MigrationRun).filter(MigrationRun.id.in_(ids)).delete(
            synchronize_session=False
        )
        s.commit()
    finally:
        s.close()


def _baseline_active() -> int:
    from db.models import MigrationRun

    s = _session()
    try:
        return s.query(MigrationRun).filter(
            MigrationRun.status.in_(["pending", "running"])
        ).count()
    finally:
        s.close()


def test_third_active_run_allowed_fourth_blocked_in_demo(monkeypatch, active_runs):
    monkeypatch.setenv("PUBLIC_DEMO_MODE", "1")
    base = _baseline_active()
    assert base == 0, "test needs a DB with no active runs"
    active_runs(2)
    with patch("app.tasks.start_migration_run", return_value="00000000-0000-0000-0000-000000000001"):
        assert client.post("/runs", json=BODY).status_code == 201  # 3rd
    active_runs(1)  # now 3 active
    resp = client.post("/runs", json=BODY)
    assert resp.status_code == 429
    assert "at most 3" in resp.json()["detail"]


def test_completed_runs_do_not_count(monkeypatch, active_runs):
    monkeypatch.setenv("PUBLIC_DEMO_MODE", "1")
    active_runs(5, status="completed")
    with patch("app.tasks.start_migration_run", return_value="00000000-0000-0000-0000-000000000001"):
        assert client.post("/runs", json=BODY).status_code == 201


def test_no_cap_when_flag_unset(monkeypatch, active_runs):
    monkeypatch.delenv("PUBLIC_DEMO_MODE", raising=False)
    active_runs(5)
    with patch("app.tasks.start_migration_run", return_value="00000000-0000-0000-0000-000000000001"):
        assert client.post("/runs", json=BODY).status_code == 201
