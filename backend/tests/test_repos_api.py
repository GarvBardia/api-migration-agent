"""Tests for the `repos` REST API -- added 2026-09-16, explicitly BEYOND
the original 10-step plan (see CLAUDE.md's dated note).

Pure CRUD against the real Postgres database via the real FastAPI
`TestClient` -- no Celery worker needed here (these endpoints never
dispatch a task; that's `periodic_auto_check_sweep`'s job, tested
separately in `test_auto_check_sweep.py`).
"""

from __future__ import annotations

import uuid

from fastapi.testclient import TestClient

from app.api.main import app

client = TestClient(app)


def _session():
    from db import SessionLocal

    return SessionLocal()


def _cleanup(repo_id) -> None:
    from db.models import Repo

    session = _session()
    try:
        if repo_id is not None:
            session.query(Repo).filter_by(id=repo_id).delete(
                synchronize_session=False
            )
        session.commit()
    finally:
        session.close()


class TestCreateAndListRepos:
    def test_create_then_list_includes_it(self, tmp_path):
        repo_id = None
        try:
            resp = client.post(
                "/repos",
                json={
                    "name": "repos-api-test-create",
                    "repo_path": str(tmp_path),
                    "default_api_name": "repos-api-test-oldapi",
                },
            )
            assert resp.status_code == 201
            body = resp.json()
            repo_id = body["id"]
            assert body["name"] == "repos-api-test-create"
            assert body["repo_path"] == str(tmp_path)
            assert body["default_api_name"] == "repos-api-test-oldapi"
            # Defaults, not asked for explicitly on this request.
            assert body["auto_check_enabled"] is False
            assert body["check_interval_hours"] is None
            assert body["last_auto_checked_at"] is None

            list_resp = client.get("/repos")
            assert list_resp.status_code == 200
            names = [r["name"] for r in list_resp.json()]
            assert "repos-api-test-create" in names
        finally:
            _cleanup(repo_id)

    def test_create_with_auto_check_fields(self, tmp_path):
        repo_id = None
        try:
            resp = client.post(
                "/repos",
                json={
                    "name": "repos-api-test-autocheck",
                    "repo_path": str(tmp_path),
                    "default_api_name": "repos-api-test-otherapi",
                    "auto_check_enabled": True,
                    "check_interval_hours": 12,
                },
            )
            assert resp.status_code == 201
            body = resp.json()
            repo_id = body["id"]
            assert body["auto_check_enabled"] is True
            assert body["check_interval_hours"] == 12
        finally:
            _cleanup(repo_id)


class TestDeleteRepo:
    def test_delete_removes_it_from_list(self, tmp_path):
        resp = client.post(
            "/repos",
            json={
                "name": "repos-api-test-delete",
                "repo_path": str(tmp_path),
                "default_api_name": "repos-api-test-deleteapi",
            },
        )
        repo_id = resp.json()["id"]

        del_resp = client.delete(f"/repos/{repo_id}")
        assert del_resp.status_code == 204

        list_resp = client.get("/repos")
        ids = [r["id"] for r in list_resp.json()]
        assert repo_id not in ids

    def test_delete_unknown_id_is_404(self):
        resp = client.delete(f"/repos/{uuid.uuid4()}")
        assert resp.status_code == 404
