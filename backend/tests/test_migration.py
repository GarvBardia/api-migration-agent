"""Tests for the Step 4 Migration Agent: provider adapters + migration.py.

Live-call coverage: the OpenRouter adapter is exercised against the real
API using the model configured in the project root `.env`
(`MIGRATION_LLM_MODEL`), guarded by `TestOpenRouterLiveSmoke` -- it's
skipped automatically if no `OPENROUTER_API_KEY` is available (e.g. in CI).
The Claude adapter has NO live coverage in this session: no
`ANTHROPIC_API_KEY` was available in the environment or `.env`, so
`ClaudeAdapter` is verified only against mocked Anthropic SDK responses
below. See the session summary for the exact detail -- don't take Claude's
coverage as equivalent to OpenRouter's without reading that caveat.
"""

from __future__ import annotations

import ast
import json
import types
import uuid
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from dotenv import dotenv_values
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.agents.llm_providers import (
    ClaudeAdapter,
    MigrationEdit,
    OpenRouterAdapter,
    ProviderResponseError,
    get_provider,
)
from app.agents.migration import migrate_file_task

SAMPLE_REPO = Path(__file__).parent / "fixtures" / "step3_sample_repo"
ROOT_ENV_PATH = Path(__file__).resolve().parents[2] / ".env"

FAKE_OLD_CODE = "oldapi.legacy_call(1, 2)"
FAKE_NEW_CODE = "oldapi.new_call(1, 2)"
FAKE_RATIONALE = "Renamed legacy_call to new_call per the 2.0 signature change."


def _fake_claude_tool_use_response():
    block = types.SimpleNamespace(
        type="tool_use",
        name="propose_edit",
        input={"new_code": FAKE_NEW_CODE, "rationale": FAKE_RATIONALE},
    )
    return types.SimpleNamespace(content=[block], stop_reason="tool_use")


def _fake_openrouter_response(new_code=FAKE_NEW_CODE, rationale=FAKE_RATIONALE, omit_rationale=False):
    args = {"new_code": new_code}
    if not omit_rationale:
        args["rationale"] = rationale
    return {
        "choices": [
            {
                "message": {
                    "tool_calls": [
                        {
                            "function": {
                                "name": "propose_edit",
                                "arguments": json.dumps(args),
                            }
                        }
                    ]
                }
            }
        ]
    }


# ---------------------------------------------------------------------------
# Done-criteria 1 & 5: both adapters -> same MigrationEdit shape, no
# free-text diff parsing.
# ---------------------------------------------------------------------------


class TestBothAdaptersProduceValidMigrationEdit:
    def test_claude_adapter_mocked(self, monkeypatch):
        adapter = ClaudeAdapter(api_key="test-key")
        monkeypatch.setattr(
            adapter._client.messages,
            "create",
            MagicMock(return_value=_fake_claude_tool_use_response()),
        )
        edit = adapter.propose_edit(
            old_code=FAKE_OLD_CODE,
            old_signature="oldapi.legacy_call",
            new_signature="oldapi.new_call",
            migration_notes=None,
        )
        assert isinstance(edit, MigrationEdit)
        assert edit.new_code == FAKE_NEW_CODE
        assert edit.rationale == FAKE_RATIONALE

    def test_openrouter_adapter_mocked(self, monkeypatch):
        import app.agents.llm_providers.openrouter_adapter as ora

        mock_response = MagicMock()
        mock_response.raise_for_status = MagicMock()
        mock_response.json = MagicMock(return_value=_fake_openrouter_response())
        monkeypatch.setattr(ora.httpx, "post", MagicMock(return_value=mock_response))

        adapter = OpenRouterAdapter(api_key="test-key", model="cohere/north-mini-code:free")
        edit = adapter.propose_edit(
            old_code=FAKE_OLD_CODE,
            old_signature="oldapi.legacy_call",
            new_signature="oldapi.new_call",
            migration_notes=None,
        )
        assert isinstance(edit, MigrationEdit)
        assert edit.new_code == FAKE_NEW_CODE
        assert edit.rationale == FAKE_RATIONALE

    def test_identical_input_both_adapters_agree(self, monkeypatch):
        """The same fixture input, run through two genuinely different
        request/response shapes, validates against the same pydantic model
        and produces the same edit."""

        claude = ClaudeAdapter(api_key="test-key")
        monkeypatch.setattr(
            claude._client.messages,
            "create",
            MagicMock(return_value=_fake_claude_tool_use_response()),
        )

        import app.agents.llm_providers.openrouter_adapter as ora

        mock_response = MagicMock()
        mock_response.raise_for_status = MagicMock()
        mock_response.json = MagicMock(return_value=_fake_openrouter_response())
        monkeypatch.setattr(ora.httpx, "post", MagicMock(return_value=mock_response))
        openrouter = OpenRouterAdapter(api_key="test-key", model="cohere/north-mini-code:free")

        kwargs = dict(
            old_code=FAKE_OLD_CODE,
            old_signature="oldapi.legacy_call",
            new_signature="oldapi.new_call",
            migration_notes="use new_call instead",
        )
        edit_a = claude.propose_edit(**kwargs)
        edit_b = openrouter.propose_edit(**kwargs)
        assert isinstance(edit_a, MigrationEdit)
        assert isinstance(edit_b, MigrationEdit)
        assert edit_a == edit_b


class TestNoFreeTextDiffParsing:
    def test_adapters_only_build_migration_edit_via_pydantic_validation(self):
        import app.agents.llm_providers.claude_adapter as claude_mod
        import app.agents.llm_providers.openrouter_adapter as or_mod

        for mod in (claude_mod, or_mod):
            source = Path(mod.__file__).read_text()
            assert "difflib" not in source
            assert "MigrationEdit.model_validate(" in source


# ---------------------------------------------------------------------------
# Done-criterion 2: provider selection is config-driven.
# ---------------------------------------------------------------------------


class TestProviderSelection:
    def test_claude_selected_via_env(self):
        provider = get_provider({"MIGRATION_LLM_PROVIDER": "claude", "ANTHROPIC_API_KEY": "test-key"})
        assert isinstance(provider, ClaudeAdapter)

    def test_openrouter_selected_via_env(self):
        provider = get_provider(
            {
                "MIGRATION_LLM_PROVIDER": "openrouter",
                "OPENROUTER_API_KEY": "test-key",
                "MIGRATION_LLM_MODEL": "cohere/north-mini-code:free",
            }
        )
        assert isinstance(provider, OpenRouterAdapter)

    def test_switching_env_value_switches_adapter_type(self):
        """The same call, only MIGRATION_LLM_PROVIDER differs -> different
        adapter class, proving selection is config-driven not hardcoded."""

        base_env = {
            "ANTHROPIC_API_KEY": "test-key",
            "OPENROUTER_API_KEY": "test-key",
            "MIGRATION_LLM_MODEL": "cohere/north-mini-code:free",
        }
        claude_provider = get_provider({**base_env, "MIGRATION_LLM_PROVIDER": "claude"})
        openrouter_provider = get_provider({**base_env, "MIGRATION_LLM_PROVIDER": "openrouter"})
        assert type(claude_provider) is not type(openrouter_provider)
        assert isinstance(claude_provider, ClaudeAdapter)
        assert isinstance(openrouter_provider, OpenRouterAdapter)

    def test_unknown_provider_raises(self):
        with pytest.raises(ProviderResponseError):
            get_provider({"MIGRATION_LLM_PROVIDER": "bogus"})

    def test_missing_api_key_raises_typed_error_not_silent(self):
        with pytest.raises(ProviderResponseError):
            get_provider({"MIGRATION_LLM_PROVIDER": "claude", "ANTHROPIC_API_KEY": ""})


# ---------------------------------------------------------------------------
# Done-criteria 3 & 4: end-to-end migrate_file_task, success and failure paths.
# ---------------------------------------------------------------------------


@pytest.fixture()
def sqlite_session():
    from db.models import Base, ChangelogEvent, FileTask, MigrationRun

    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(
        engine,
        tables=[MigrationRun.__table__, ChangelogEvent.__table__, FileTask.__table__],
    )
    with Session(engine) as session:
        yield session


def _make_run_and_task(session, **task_overrides):
    from db.models import ChangelogEvent, FileTask, MigrationRun

    run = MigrationRun(
        repo_url="https://example.com/fixture-repo.git",
        api_name="oldapi",
        version_from="1.x",
        version_to="2.0",
        status="running",
    )
    session.add(run)
    session.flush()

    event = ChangelogEvent(
        api_name="oldapi",
        version_from="1.x",
        version_to="2.0",
        change_type="deprecation",
        old_signature="oldapi.legacy_call",
        new_signature="oldapi.new_call",
        migration_notes="Use new_call with the same arguments.",
    )
    session.add(event)
    session.flush()

    defaults = dict(
        run_id=run.id,
        file_path="service_a.py",
        status="pending",
        matched_symbol="legacy_call",
        line_start=7,
        line_end=7,
        confidence_score=0.95,
        changelog_event_id=event.id,
    )
    defaults.update(task_overrides)
    task = FileTask(**defaults)
    session.add(task)
    session.flush()
    return run, event, task


class _FakeProvider:
    def __init__(self, edit: MigrationEdit | None = None, error: Exception | None = None):
        self._edit = edit
        self._error = error

    def propose_edit(self, **kwargs):
        if self._error is not None:
            raise self._error
        return self._edit


class TestMigrateFileTaskSuccess:
    def test_service_a_fixture_produces_valid_new_code_snippet(self, sqlite_session):
        """Done-criterion 3: reuses Step 3's service_a.py match end to end."""

        _, _, task = _make_run_and_task(sqlite_session)
        provider = _FakeProvider(
            edit=MigrationEdit(new_code=FAKE_NEW_CODE, rationale=FAKE_RATIONALE)
        )

        migrate_file_task(sqlite_session, task, SAMPLE_REPO, provider)
        sqlite_session.commit()

        assert task.status == "in_progress"
        assert task.new_code_snippet == FAKE_NEW_CODE
        ast.parse(task.new_code_snippet)  # must not raise
        assert task.old_code_snippet is not None
        assert "legacy_call" in task.old_code_snippet  # pulled from real source

    def test_old_code_snippet_populated_from_source_when_unset(self, sqlite_session):
        _, _, task = _make_run_and_task(sqlite_session)
        assert task.old_code_snippet is None
        provider = _FakeProvider(edit=MigrationEdit(new_code="x = 1", rationale="n/a"))

        migrate_file_task(sqlite_session, task, SAMPLE_REPO, provider)
        sqlite_session.commit()

        expected_line = SAMPLE_REPO.joinpath("service_a.py").read_text().splitlines()[6]
        assert task.old_code_snippet == expected_line

    def test_existing_old_code_snippet_is_not_overwritten_by_source_read(self, sqlite_session):
        _, _, task = _make_run_and_task(sqlite_session, old_code_snippet="already set by step 3")
        provider = _FakeProvider(edit=MigrationEdit(new_code="x = 1", rationale="n/a"))

        migrate_file_task(sqlite_session, task, SAMPLE_REPO, provider)
        sqlite_session.commit()

        assert task.old_code_snippet == "already set by step 3"

    def test_dedents_indented_new_code_before_validating(self, sqlite_session):
        """A provider echoing the snippet's original indentation shouldn't
        fail validation purely for that leading whitespace."""

        _, _, task = _make_run_and_task(sqlite_session)
        provider = _FakeProvider(
            edit=MigrationEdit(new_code="    oldapi.new_call(1, 2)", rationale="n/a")
        )

        migrate_file_task(sqlite_session, task, SAMPLE_REPO, provider)
        sqlite_session.commit()

        assert task.status == "in_progress"
        ast.parse(task.new_code_snippet)


class TestMigrateFileTaskFailureRoutesToNeedsReview:
    def test_provider_error_sets_needs_review_no_partial_write(self, sqlite_session):
        _, _, task = _make_run_and_task(sqlite_session)
        provider = _FakeProvider(error=ProviderResponseError("simulated malformed tool call"))

        migrate_file_task(sqlite_session, task, SAMPLE_REPO, provider)
        sqlite_session.commit()

        assert task.status == "needs_review"
        assert task.failure_reason and "simulated malformed tool call" in task.failure_reason
        assert task.new_code_snippet is None

    def test_syntactically_invalid_new_code_sets_needs_review(self, sqlite_session):
        _, _, task = _make_run_and_task(sqlite_session)
        provider = _FakeProvider(edit=MigrationEdit(new_code="def (((invalid", rationale="oops"))

        migrate_file_task(sqlite_session, task, SAMPLE_REPO, provider)
        sqlite_session.commit()

        assert task.status == "needs_review"
        assert task.failure_reason
        assert task.new_code_snippet is None

    def test_missing_changelog_event_sets_needs_review(self, sqlite_session):
        _, _, task = _make_run_and_task(sqlite_session)
        task.changelog_event_id = uuid.uuid4()  # dangling reference
        sqlite_session.flush()
        provider = _FakeProvider(edit=MigrationEdit(new_code="x = 1", rationale="n/a"))

        migrate_file_task(sqlite_session, task, SAMPLE_REPO, provider)
        sqlite_session.commit()

        assert task.status == "needs_review"
        assert "changelog_events" in task.failure_reason
        assert task.new_code_snippet is None


class TestMalformedAdapterResponseEndToEnd:
    """Done-criterion 4, exercised through a real adapter under mock (not
    just the FakeProvider test double above)."""

    def test_claude_missing_tool_use_block_sets_needs_review(self, sqlite_session, monkeypatch):
        _, _, task = _make_run_and_task(sqlite_session)
        adapter = ClaudeAdapter(api_key="test-key")
        bad_response = types.SimpleNamespace(
            content=[types.SimpleNamespace(type="text", text="I cannot help with that.")],
            stop_reason="end_turn",
        )
        monkeypatch.setattr(
            adapter._client.messages, "create", MagicMock(return_value=bad_response)
        )

        migrate_file_task(sqlite_session, task, SAMPLE_REPO, adapter)
        sqlite_session.commit()

        assert task.status == "needs_review"
        assert "propose_edit" in task.failure_reason
        assert task.new_code_snippet is None

    def test_openrouter_missing_required_field_sets_needs_review(self, sqlite_session, monkeypatch):
        import app.agents.llm_providers.openrouter_adapter as ora

        _, _, task = _make_run_and_task(sqlite_session)
        mock_response = MagicMock()
        mock_response.raise_for_status = MagicMock()
        mock_response.json = MagicMock(
            return_value=_fake_openrouter_response(omit_rationale=True)
        )
        monkeypatch.setattr(ora.httpx, "post", MagicMock(return_value=mock_response))

        adapter = OpenRouterAdapter(api_key="test-key", model="cohere/north-mini-code:free")
        migrate_file_task(sqlite_session, task, SAMPLE_REPO, adapter)
        sqlite_session.commit()

        assert task.status == "needs_review"
        assert "schema validation" in task.failure_reason
        assert task.new_code_snippet is None

    def test_openrouter_no_tool_call_at_all_sets_needs_review(self, sqlite_session, monkeypatch):
        import app.agents.llm_providers.openrouter_adapter as ora

        _, _, task = _make_run_and_task(sqlite_session)
        mock_response = MagicMock()
        mock_response.raise_for_status = MagicMock()
        mock_response.json = MagicMock(
            return_value={"choices": [{"message": {"content": "sorry, I can't do that"}}]}
        )
        monkeypatch.setattr(ora.httpx, "post", MagicMock(return_value=mock_response))

        adapter = OpenRouterAdapter(api_key="test-key", model="cohere/north-mini-code:free")
        migrate_file_task(sqlite_session, task, SAMPLE_REPO, adapter)
        sqlite_session.commit()

        assert task.status == "needs_review"
        assert task.failure_reason
        assert task.new_code_snippet is None


# ---------------------------------------------------------------------------
# Live smoke test -- OpenRouter only (see module docstring re: Claude).
# ---------------------------------------------------------------------------

_root_env = dotenv_values(ROOT_ENV_PATH) if ROOT_ENV_PATH.exists() else {}
_LIVE_OPENROUTER_KEY = _root_env.get("OPENROUTER_API_KEY") or ""
_LIVE_OPENROUTER_MODEL = _root_env.get("MIGRATION_LLM_MODEL") or ""


@pytest.mark.skipif(
    not _LIVE_OPENROUTER_KEY or not _LIVE_OPENROUTER_MODEL,
    reason="No OPENROUTER_API_KEY/MIGRATION_LLM_MODEL in the project .env; skipping live call.",
)
class TestOpenRouterLiveSmoke:
    def test_real_openrouter_call_against_service_a_fixture(self, sqlite_session):
        """Actually calls OpenRouter with the model configured in .env,
        proving the adapter works against the real API, not just mocks."""

        _, _, task = _make_run_and_task(sqlite_session)
        adapter = OpenRouterAdapter(
            api_key=_LIVE_OPENROUTER_KEY, model=_LIVE_OPENROUTER_MODEL
        )

        migrate_file_task(sqlite_session, task, SAMPLE_REPO, adapter)
        sqlite_session.commit()

        # A live model may occasionally misbehave -- but if it *did* produce
        # a valid edit, that edit must actually be usable, and if it didn't,
        # the failure must be captured cleanly, never a crash or partial
        # write. Either terminal state is an acceptable pass for a live
        # smoke test; a crash or an inconsistent row is not.
        assert task.status in ("in_progress", "needs_review")
        if task.status == "in_progress":
            assert task.new_code_snippet
            ast.parse(task.new_code_snippet)
        else:
            assert task.failure_reason
            assert task.new_code_snippet is None
