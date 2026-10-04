"""Tests for repo-path validation and ALLOWED_REPO_ROOTS confinement
(added 2026-10-04, public-demo hardening)."""

from __future__ import annotations

import os
import uuid

import pytest
from fastapi.testclient import TestClient

from app.api.main import app
from app.repo_paths import RepoPathError, validate_repo_path

client = TestClient(app)


@pytest.fixture
def root(tmp_path, monkeypatch):
    """A confinement root containing one repo folder."""
    r = tmp_path / "root"
    (r / "repo").mkdir(parents=True)
    monkeypatch.setenv("ALLOWED_REPO_ROOTS", str(r))
    return r


class TestValidateRepoPath:
    def test_inside_root_accepted(self, root):
        assert validate_repo_path(str(root / "repo")) == (root / "repo").resolve()

    def test_root_itself_accepted(self, root):
        assert validate_repo_path(str(root)) == root.resolve()

    def test_dotdot_escape_rejected(self, root, tmp_path):
        (tmp_path / "outside").mkdir()
        sneaky = str(root / "repo" / ".." / ".." / "outside")
        with pytest.raises(RepoPathError, match="only accepts folders inside"):
            validate_repo_path(sneaky)

    def test_symlink_escape_rejected(self, root, tmp_path):
        outside = tmp_path / "outside"
        outside.mkdir()
        link = root / "link"
        try:
            os.symlink(outside, link, target_is_directory=True)
        except (OSError, NotImplementedError):
            pytest.skip("symlinks not permitted on this system")
        with pytest.raises(RepoPathError, match="only accepts folders inside"):
            validate_repo_path(str(link))

    def test_nonexistent_rejected(self, root):
        with pytest.raises(RepoPathError, match="does not exist"):
            validate_repo_path(str(root / "nope"))

    def test_file_instead_of_folder_rejected(self, root):
        f = root / "a.py"
        f.write_text("x = 1\n")
        with pytest.raises(RepoPathError, match="not a folder"):
            validate_repo_path(str(f))

    def test_empty_rejected(self, root):
        with pytest.raises(RepoPathError):
            validate_repo_path("  ")


class TestUnsetLeavesDevBehaviorUnchanged:
    def test_any_existing_dir_accepted_when_unset(self, tmp_path, monkeypatch):
        monkeypatch.delenv("ALLOWED_REPO_ROOTS", raising=False)
        assert validate_repo_path(str(tmp_path)) == tmp_path.resolve()

    def test_nonexistent_still_rejected_when_unset(self, tmp_path, monkeypatch):
        monkeypatch.delenv("ALLOWED_REPO_ROOTS", raising=False)
        with pytest.raises(RepoPathError, match="does not exist"):
            validate_repo_path(str(tmp_path / "nope"))


class TestApiReturns422:
    def test_runs_outside_root(self, root, tmp_path):
        (tmp_path / "outside").mkdir()
        resp = client.post(
            "/runs",
            json={
                "repo_url": str(tmp_path / "outside"),
                "api_name": "x",
                "version_from": "1",
                "version_to": "2",
            },
        )
        assert resp.status_code == 422
        assert "only accepts folders inside" in resp.json()["detail"]

    def test_runs_nonexistent(self, tmp_path, monkeypatch):
        monkeypatch.delenv("ALLOWED_REPO_ROOTS", raising=False)
        resp = client.post(
            "/runs",
            json={
                "repo_url": str(tmp_path / "nope"),
                "api_name": "x",
                "version_from": "1",
                "version_to": "2",
            },
        )
        assert resp.status_code == 422
        assert "does not exist" in resp.json()["detail"]

    def test_repos_outside_root(self, root, tmp_path):
        (tmp_path / "outside").mkdir()
        resp = client.post(
            "/repos",
            json={
                "name": "paths-test-outside",
                "repo_path": str(tmp_path / "outside"),
                "default_api_name": "x",
            },
        )
        assert resp.status_code == 422

    def test_repos_file_rejected(self, tmp_path, monkeypatch):
        monkeypatch.delenv("ALLOWED_REPO_ROOTS", raising=False)
        f = tmp_path / "a.py"
        f.write_text("x = 1\n")
        resp = client.post(
            "/repos",
            json={
                "name": "paths-test-file",
                "repo_path": str(f),
                "default_api_name": "x",
            },
        )
        assert resp.status_code == 422


class TestScanTaskFailsBadPaths:
    """scan_task must mark the run failed, never completed."""

    def _run_scan(self, repo_url: str) -> str:
        from app.tasks import scan_task
        from db import SessionLocal
        from db.models import MigrationRun

        session = SessionLocal()
        run_id = None
        try:
            run = MigrationRun(
                repo_url=repo_url,
                api_name=f"paths-test-{uuid.uuid4()}",
                version_from="1",
                version_to="2",
                status="pending",
            )
            session.add(run)
            session.commit()
            run_id = run.id
            with pytest.raises(RepoPathError):
                scan_task.run(str(run_id))
            session.expire_all()
            return session.get(MigrationRun, run_id).status
        finally:
            if run_id is not None:
                session.query(MigrationRun).filter_by(id=run_id).delete(
                    synchronize_session=False
                )
                session.commit()
            session.close()

    def test_nonexistent_marks_failed(self, tmp_path, monkeypatch):
        monkeypatch.delenv("ALLOWED_REPO_ROOTS", raising=False)
        assert self._run_scan(str(tmp_path / "nope")) == "failed"

    def test_outside_root_marks_failed(self, root, tmp_path):
        (tmp_path / "outside").mkdir()
        assert self._run_scan(str(tmp_path / "outside")) == "failed"
