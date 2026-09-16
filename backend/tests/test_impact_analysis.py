"""Tests for the Step 3 Impact Analysis Agent (tree-sitter AST scanner).

Ground truth for the matching tests lives in
``tests/fixtures/step3_expected.json`` — a hand-labeled expected-results
file, not something eyeballed from the scanner's own output.

Persistence/idempotency tests (``TestPersistMatches``) use an in-memory
SQLite database standing in for Postgres. This is a deliberate substitution
documented in the run-while-away summary: Docker Desktop is not installed
on this machine, so the real Postgres instance from docker-compose could
not be started or verified against. SQLite supports the same
``UniqueConstraint``/``CheckConstraint`` machinery SQLAlchemy emits for
``migration_runs``/``changelog_events``/``file_tasks`` (everything except
the pgvector-typed ``api_doc_chunks`` table, which these tests don't need),
so the upsert/idempotency logic in ``persist_matches`` is exercised
end-to-end against a real constraint, not mocked out. It is NOT a
substitute for running ``alembic upgrade head`` against real Postgres and
confirming the schema there — that step is still outstanding.
"""

from __future__ import annotations

import json
import uuid
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.agents.impact_analysis import (
    Match,
    find_matches_in_file,
    persist_matches,
    scan_repo,
    should_skip_path,
)

FIXTURES_DIR = Path(__file__).parent / "fixtures"
SAMPLE_REPO = FIXTURES_DIR / "step3_sample_repo"
EXPECTED_PATH = FIXTURES_DIR / "step3_expected.json"

CONFIDENCE_TIER_RANGES = {
    "high": (0.8, 1.0),
    "medium": (0.4, 0.8),
    "low": (0.0, 0.4),
}


class _FakeChangelogEvent:
    """Plain stand-in for a ``ChangelogEvent`` ORM row.

    ``find_matches_in_file``/``scan_repo`` only need ``.id``/``.old_signature``
    (see ``ChangelogEventLike`` in impact_analysis.py) so tests don't need a
    live DB just to exercise the matching logic.
    """

    def __init__(self, id: uuid.UUID, old_signature: str):
        self.id = id
        self.old_signature = old_signature


def _load_expected() -> dict:
    return json.loads(EXPECTED_PATH.read_text())


def _changelog_events_from_expected(expected: dict) -> dict[str, _FakeChangelogEvent]:
    """key -> fake ChangelogEvent, with a stable UUID derived from the key."""

    events = {}
    for row in expected["changelog_events"]:
        event_id = uuid.uuid5(uuid.NAMESPACE_DNS, row["key"])
        events[row["key"]] = _FakeChangelogEvent(event_id, row["old_signature"])
    return events


def _actual_match_tuples(matches: list[Match]) -> set[tuple]:
    return {
        (m.file_path, m.matched_symbol, m.line_start, m.line_end)
        for m in matches
    }


def _expected_match_tuples(expected: dict) -> set[tuple]:
    return {
        (m["file_path"], m["matched_symbol"], m["line_start"], m["line_end"])
        for m in expected["matches"]
    }


class TestScanRepoAgainstFixture:
    """Done-criteria 1, 3, 4, 5: exact recall/precision against ground truth."""

    def setup_method(self):
        self.expected = _load_expected()
        self.events_by_key = _changelog_events_from_expected(self.expected)
        self.matches = scan_repo(SAMPLE_REPO, list(self.events_by_key.values()))

    def test_exact_match_set(self):
        """No missed matches, no false positives -- verified against ground truth."""

        assert _actual_match_tuples(self.matches) == _expected_match_tuples(
            self.expected
        )

    def test_confidence_tiers_match_expected(self):
        by_loc = {
            (m.file_path, m.matched_symbol, m.line_start): m
            for m in self.matches
        }
        for row in self.expected["matches"]:
            key = (row["file_path"], row["matched_symbol"], row["line_start"])
            actual = by_loc[key]
            lo, hi = CONFIDENCE_TIER_RANGES[row["confidence_tier"]]
            assert lo <= actual.confidence_score <= hi, (
                f"{key} expected tier {row['confidence_tier']} "
                f"({lo}-{hi}) but got {actual.confidence_score}"
            )

    def test_unrelated_file_has_zero_matches(self):
        """Local same-named function must not be matched (scope/import resolution,
        not bare-name matching)."""

        unrelated_matches = [m for m in self.matches if m.file_path == "unrelated.py"]
        assert unrelated_matches == []

    def test_multi_match_produces_two_distinct_rows(self):
        rows = [m for m in self.matches if m.file_path == "multi_match.py"]
        assert len(rows) == 2
        symbols = {m.matched_symbol for m in rows}
        assert symbols == {"legacy_call", "old_thing"}

    def test_dynamic_dispatch_present_with_low_confidence(self):
        rows = [m for m in self.matches if m.file_path == "dynamic_dispatch.py"]
        assert len(rows) == 1
        assert rows[0].confidence_score < 0.4

    def test_aliased_import_resolves_to_same_symbol_as_direct_calls(self):
        direct = next(
            m for m in self.matches if m.file_path == "service_a.py"
        )
        aliased = next(
            m for m in self.matches if m.file_path == "service_b.py"
        )
        assert aliased.matched_symbol == direct.matched_symbol == "legacy_call"

    def test_changelog_event_id_wired_to_correct_row(self):
        legacy_id = self.events_by_key["legacy_call"].id
        old_thing_id = self.events_by_key["old_thing"].id
        for m in self.matches:
            if m.matched_symbol == "legacy_call":
                assert m.changelog_event_id == legacy_id
            elif m.matched_symbol == "old_thing":
                assert m.changelog_event_id == old_thing_id


class TestNoRegexInMatchingPath:
    """Done-criterion 7: no regex anywhere in the AST-matching code path."""

    def test_no_re_module_import(self):
        source = Path("app/agents/impact_analysis.py").read_text()
        assert "import re" not in source, (
            "impact_analysis.py must not use the `re` module for AST matching "
            "(CLAUDE.md #4.2 — regex is only permitted for trivial filename "
            "filtering, and this module doesn't even need that)"
        )


class TestShouldSkipPath:
    def test_skips_venv_and_vcs_dirs(self):
        assert should_skip_path(Path("venv/lib/foo.py"))
        assert should_skip_path(Path(".git/hooks/foo.py"))
        assert should_skip_path(Path("node_modules/x/foo.py"))

    def test_skips_own_fixtures_dir(self):
        assert should_skip_path(Path("tests/fixtures/step3_sample_repo/service_a.py"))

    def test_does_not_skip_ordinary_source(self):
        assert not should_skip_path(Path("app/agents/impact_analysis.py"))

    def test_skips_non_python_files(self):
        assert should_skip_path(Path("README.md"))


class TestAdditionalMatchForms:
    """Forms called out in TASKS.md/kickoff prompt but not covered by a
    dedicated fixture file: wildcard imports and instantiation-style calls.
    """

    def test_wildcard_import_matches_bare_name(self):
        from app.agents.impact_analysis import TargetSymbol

        event = _FakeChangelogEvent(uuid.uuid4(), "oldapi.legacy_call")
        source = b"from oldapi import *\n\nlegacy_call(1)\n"
        matches = find_matches_in_file(
            "wildcard.py", source, [TargetSymbol.from_changelog_event(event)]
        )
        assert len(matches) == 1
        assert matches[0].matched_symbol == "legacy_call"
        assert 0.4 <= matches[0].confidence_score < 0.8

    def test_wildcard_import_does_not_match_unrelated_module(self):
        from app.agents.impact_analysis import TargetSymbol

        event = _FakeChangelogEvent(uuid.uuid4(), "oldapi.legacy_call")
        source = b"from otherlib import *\n\nlegacy_call(1)\n"
        matches = find_matches_in_file(
            "wildcard_other.py", source, [TargetSymbol.from_changelog_event(event)]
        )
        assert matches == []

    def test_instantiation_call_is_matched(self):
        """A deprecated class instantiation (`module.OldThing(...)`) is
        syntactically identical to a function call in the AST -- confirms it
        isn't accidentally excluded by some function-only filter."""

        from app.agents.impact_analysis import TargetSymbol

        event = _FakeChangelogEvent(uuid.uuid4(), "oldapi.OldThing")
        source = b"import oldapi\n\nobj = oldapi.OldThing(1, 2)\n"
        matches = find_matches_in_file(
            "instantiate.py", source, [TargetSymbol.from_changelog_event(event)]
        )
        assert len(matches) == 1
        assert matches[0].matched_symbol == "OldThing"
        assert matches[0].confidence_score >= 0.8


@pytest.fixture()
def sqlite_session():
    """In-memory SQLite session with migration_runs/changelog_events/file_tasks.

    See the module docstring for why SQLite stands in for Postgres here.
    """

    from db.models import Base, ChangelogEvent, FileTask, MigrationRun

    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(
        engine,
        tables=[
            MigrationRun.__table__,
            ChangelogEvent.__table__,
            FileTask.__table__,
        ],
    )
    with Session(engine) as session:
        yield session


class TestPersistMatches:
    """Done-criteria 2, 6: match-level persistence and idempotent re-scan."""

    def _make_run_and_events(self, session):
        from db.models import ChangelogEvent, MigrationRun

        run = MigrationRun(
            repo_url="https://example.com/fixture-repo.git",
            api_name="oldapi",
            version_from="1.x",
            version_to="2.0",
            status="running",
        )
        session.add(run)
        session.flush()

        expected = _load_expected()
        events = {}
        for row in expected["changelog_events"]:
            ev = ChangelogEvent(
                api_name=row["api_name"],
                version_from=row["version_from"],
                version_to=row["version_to"],
                change_type=row["change_type"],
                old_signature=row["old_signature"],
                new_signature=row["new_signature"],
            )
            session.add(ev)
            events[row["key"]] = ev
        session.flush()
        return run, events

    def test_multi_match_rows_both_persisted(self, sqlite_session):
        from db.models import FileTask

        run, events = self._make_run_and_events(sqlite_session)
        matches = scan_repo(SAMPLE_REPO, list(events.values()))
        inserted = persist_matches(sqlite_session, run.id, matches)
        sqlite_session.commit()

        assert inserted == len(matches)

        rows = (
            sqlite_session.query(FileTask)
            .filter_by(run_id=run.id, file_path="multi_match.py")
            .all()
        )
        assert len(rows) == 2
        assert {r.matched_symbol for r in rows} == {"legacy_call", "old_thing"}
        assert {r.id for r in rows}.__len__() == 2  # distinct rows, not one reused

    def test_rescan_is_idempotent(self, sqlite_session):
        from db.models import FileTask

        run, events = self._make_run_and_events(sqlite_session)
        matches = scan_repo(SAMPLE_REPO, list(events.values()))

        first_inserted = persist_matches(sqlite_session, run.id, matches)
        sqlite_session.commit()
        first_count = sqlite_session.query(FileTask).filter_by(run_id=run.id).count()

        second_inserted = persist_matches(sqlite_session, run.id, matches)
        sqlite_session.commit()
        second_count = sqlite_session.query(FileTask).filter_by(run_id=run.id).count()

        assert first_inserted == len(matches)
        assert second_inserted == 0
        assert first_count == second_count == len(matches)

    def test_persisted_rows_have_pending_status_and_confidence(self, sqlite_session):
        from db.models import FileTask

        run, events = self._make_run_and_events(sqlite_session)
        matches = scan_repo(SAMPLE_REPO, list(events.values()))
        persist_matches(sqlite_session, run.id, matches)
        sqlite_session.commit()

        rows = sqlite_session.query(FileTask).filter_by(run_id=run.id).all()
        assert len(rows) == len(matches)
        for row in rows:
            assert row.status == "pending"
            assert row.confidence_score is not None
            assert row.line_start is not None
            assert row.changelog_event_id is not None
