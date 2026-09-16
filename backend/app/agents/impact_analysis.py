"""Impact Analysis Agent — tree-sitter AST scanner (Step 3).

Given a target repo checked out locally and the ``changelog_events`` rows
describing breaking changes for one API version bump, this module finds
every AST-level reference to each event's ``old_signature`` and turns each
individual match into a ``file_tasks`` row.

Two halves, deliberately separate:

- ``scan_repo`` / ``find_matches_in_file`` — pure, DB-independent. Parses
  files with tree-sitter and returns ``Match`` objects. No SQLAlchemy
  session involved, so this half is unit-testable against the fixture repo
  without any database at all.
- ``persist_matches`` — the only part that touches the DB. Upserts
  ``Match`` objects into ``file_tasks``, relying on the
  ``uq_file_tasks_run_path_symbol_line`` constraint (added in migration
  ``0002``) for idempotency on re-scan.

AST-first, never regex, per ``CLAUDE.md`` §4.2 — the one exception allowed
there is trivial filename filtering (``_should_skip_path``), which is not
part of the code-matching path.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Protocol

from tree_sitter import Language, Node, Parser
import tree_sitter_python as tspython

PY_LANGUAGE = Language(tspython.language())

# Directories never scanned as "target repo" source, regardless of which
# repo is being analyzed.
SKIP_DIR_NAMES = {
    "venv",
    ".venv",
    "env",
    "node_modules",
    ".git",
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
}

# Never scan this project's own fixture repo as if it were a migration
# target — matched by path suffix so it's skipped regardless of the repo
# root passed in.
_OWN_FIXTURES_MARKER = Path("tests") / "fixtures"

CONFIDENCE_DIRECT = 0.95
CONFIDENCE_ALIAS_OR_REEXPORT = 0.6
CONFIDENCE_DYNAMIC = 0.2


class ChangelogEventLike(Protocol):
    """Minimal shape this module needs from a ``ChangelogEvent`` row.

    A ``Protocol`` (structural typing) rather than importing the ORM class
    directly, so the matching half of this module has zero dependency on
    SQLAlchemy/the DB — tests can pass plain namedtuples.
    """

    id: uuid.UUID
    old_signature: str


@dataclass(frozen=True)
class TargetSymbol:
    """A breaking-change symbol to search for, parsed from ``old_signature``.

    Convention: ``old_signature`` is a dotted ``<module>.<symbol>`` path,
    e.g. ``"oldapi.legacy_call"`` — the module/package the symbol is
    imported from, and the symbol name itself.
    """

    changelog_event_id: uuid.UUID
    module: str
    symbol: str
    raw: str

    @classmethod
    def from_changelog_event(cls, event: ChangelogEventLike) -> "TargetSymbol":
        raw = event.old_signature.strip()
        if "." not in raw:
            raise ValueError(
                f"old_signature {raw!r} must be dotted as '<module>.<symbol>' "
                "(e.g. 'oldapi.legacy_call')"
            )
        module, symbol = raw.rsplit(".", 1)
        return cls(
            changelog_event_id=event.id, module=module, symbol=symbol, raw=raw
        )


@dataclass(frozen=True)
class Match:
    """One AST-level reference to a target symbol — becomes one file_tasks row."""

    file_path: str
    matched_symbol: str
    line_start: int
    line_end: int
    confidence_score: float
    changelog_event_id: uuid.UUID


@dataclass(frozen=True)
class _Binding:
    """A local name's resolved origin within one file's module scope."""

    module: str
    symbol: str | None  # None => whole-module import; attribute access needed.
    kind: str  # 'direct' | 'aliased' | 'wildcard' | 'reexport'


def _text(node: Node, source: bytes) -> str:
    return source[node.start_byte : node.end_byte].decode("utf8")


def _iter_all(node: Node) -> Iterable[Node]:
    yield node
    for child in node.children:
        yield from _iter_all(child)


def should_skip_path(path: Path) -> bool:
    """Filename/directory filtering only — not part of the AST-matching path.

    Skips known non-target directories and this project's own fixture repo
    (so Step 3's tests never get scanned as if they were the migration
    target).
    """

    parts = path.parts
    if any(p in SKIP_DIR_NAMES for p in parts):
        return True
    posix = path.as_posix()
    if f"{_OWN_FIXTURES_MARKER.as_posix()}/" in posix:
        return True
    if path.suffix != ".py":
        return True
    return False


def _build_bindings(root: Node, source: bytes) -> tuple[dict[str, _Binding], list[str]]:
    """Resolve module-level import + simple re-export bindings.

    Only resolves what's statically unambiguous at module scope: import
    statements (plain, aliased, from-import, aliased from-import,
    wildcard), plus a single-level top-level ``name = <already-bound-name>``
    or ``name = <module>.<symbol>`` re-export assignment. Anything requiring
    real dataflow (conditional rebinding, function-local aliasing, values
    stored in containers) is intentionally left unresolved — those calls
    fall through to the dynamic-dispatch handling instead, which is the
    correct behavior per CLAUDE.md's "surface as low-confidence, never
    regex-guess" rule.

    Returns ``(bindings, wildcard_modules)``.
    """

    bindings: dict[str, _Binding] = {}
    wildcard_modules: list[str] = []

    for node in root.children:
        if node.type == "import_statement":
            for child in node.children:
                if child.type == "dotted_name":
                    mod = _text(child, source)
                    local = mod.split(".")[0]
                    bindings[local] = _Binding(module=mod, symbol=None, kind="direct")
                elif child.type == "aliased_import":
                    name_node = child.child_by_field_name("name")
                    alias_node = child.child_by_field_name("alias")
                    if name_node is None or alias_node is None:
                        continue
                    mod = _text(name_node, source)
                    local = _text(alias_node, source)
                    bindings[local] = _Binding(module=mod, symbol=None, kind="aliased")

        elif node.type == "import_from_statement":
            module_node = node.child_by_field_name("module_name")
            if module_node is None:
                continue
            module = _text(module_node, source)
            for child in node.children:
                if child is module_node:
                    continue
                if child.type == "dotted_name":
                    sym = _text(child, source)
                    bindings[sym] = _Binding(module=module, symbol=sym, kind="direct")
                elif child.type == "aliased_import":
                    name_node = child.child_by_field_name("name")
                    alias_node = child.child_by_field_name("alias")
                    if name_node is None or alias_node is None:
                        continue
                    sym = _text(name_node, source)
                    local = _text(alias_node, source)
                    bindings[local] = _Binding(module=module, symbol=sym, kind="aliased")
                elif child.type == "wildcard_import":
                    wildcard_modules.append(module)

        elif node.type == "expression_statement" and node.children:
            assign = node.children[0]
            if assign.type != "assignment":
                continue
            left = assign.child_by_field_name("left")
            right = assign.child_by_field_name("right")
            if left is None or right is None or left.type != "identifier":
                continue
            local = _text(left, source)
            if right.type == "identifier":
                origin = bindings.get(_text(right, source))
                if origin is not None:
                    bindings[local] = _Binding(
                        module=origin.module, symbol=origin.symbol, kind="reexport"
                    )
            elif right.type == "attribute":
                obj = right.child_by_field_name("object")
                attr = right.child_by_field_name("attribute")
                if obj is not None and attr is not None and obj.type == "identifier":
                    base = bindings.get(_text(obj, source))
                    if base is not None and base.symbol is None:
                        bindings[local] = _Binding(
                            module=base.module,
                            symbol=_text(attr, source),
                            kind="reexport",
                        )

    return bindings, wildcard_modules


def _line_span(node: Node) -> tuple[int, int]:
    # tree-sitter rows are 0-indexed; file_tasks.line_start/line_end are 1-indexed.
    return node.start_point[0] + 1, node.end_point[0] + 1


def find_matches_in_file(
    file_path: str, source: bytes, targets: list[TargetSymbol]
) -> list[Match]:
    """Find every match of every ``targets`` entry within one file's source."""

    parser = Parser(PY_LANGUAGE)
    tree = parser.parse(source)
    root = tree.root_node
    bindings, wildcard_modules = _build_bindings(root, source)

    by_symbol: dict[str, list[TargetSymbol]] = {}
    for t in targets:
        by_symbol.setdefault(t.symbol, []).append(t)

    matches: list[Match] = []

    for node in _iter_all(root):
        if node.type != "call":
            continue
        func = node.child_by_field_name("function")
        if func is None:
            continue

        # --- dynamic dispatch: getattr(x, "symbol")(...) -----------------
        if func.type == "call":
            inner_func = func.child_by_field_name("function")
            inner_args = func.child_by_field_name("arguments")
            if (
                inner_func is not None
                and inner_func.type == "identifier"
                and _text(inner_func, source) == "getattr"
                and inner_args is not None
            ):
                str_args = [c for c in inner_args.children if c.type == "string"]
                if str_args:
                    literal = "".join(
                        _text(c, source)
                        for c in str_args[0].children
                        if c.type == "string_content"
                    )
                    for t in by_symbol.get(literal, []):
                        start, end = _line_span(node)
                        matches.append(
                            Match(
                                file_path=file_path,
                                matched_symbol=t.symbol,
                                line_start=start,
                                line_end=end,
                                confidence_score=CONFIDENCE_DYNAMIC,
                                changelog_event_id=t.changelog_event_id,
                            )
                        )
            continue

        # --- direct/aliased/re-exported name call: f(...) ----------------
        if func.type == "identifier":
            name = _text(func, source)
            binding = bindings.get(name)
            if binding is not None and binding.symbol is not None:
                for t in by_symbol.get(binding.symbol, []):
                    if t.module != binding.module:
                        continue
                    start, end = _line_span(node)
                    confidence = (
                        CONFIDENCE_DIRECT
                        if binding.kind == "direct"
                        else CONFIDENCE_ALIAS_OR_REEXPORT
                    )
                    matches.append(
                        Match(
                            file_path=file_path,
                            matched_symbol=t.symbol,
                            line_start=start,
                            line_end=end,
                            confidence_score=confidence,
                            changelog_event_id=t.changelog_event_id,
                        )
                    )
                continue
            # No import binding for this bare name — check wildcard imports.
            if binding is None:
                for wmod in wildcard_modules:
                    for t in by_symbol.get(name, []):
                        if t.module != wmod:
                            continue
                        start, end = _line_span(node)
                        matches.append(
                            Match(
                                file_path=file_path,
                                matched_symbol=t.symbol,
                                line_start=start,
                                line_end=end,
                                confidence_score=CONFIDENCE_ALIAS_OR_REEXPORT,
                                changelog_event_id=t.changelog_event_id,
                            )
                        )
            continue

        # --- attribute access call: module.symbol(...) -------------------
        if func.type == "attribute":
            obj = func.child_by_field_name("object")
            attr = func.child_by_field_name("attribute")
            if obj is None or attr is None or obj.type != "identifier":
                continue
            base_binding = bindings.get(_text(obj, source))
            if base_binding is None or base_binding.symbol is not None:
                continue
            attr_name = _text(attr, source)
            for t in by_symbol.get(attr_name, []):
                if t.module != base_binding.module:
                    continue
                start, end = _line_span(node)
                confidence = (
                    CONFIDENCE_DIRECT
                    if base_binding.kind == "direct"
                    else CONFIDENCE_ALIAS_OR_REEXPORT
                )
                matches.append(
                    Match(
                        file_path=file_path,
                        matched_symbol=t.symbol,
                        line_start=start,
                        line_end=end,
                        confidence_score=confidence,
                        changelog_event_id=t.changelog_event_id,
                    )
                )

    return matches


def scan_repo(
    repo_root: Path, changelog_events: list[ChangelogEventLike]
) -> list[Match]:
    """Walk ``repo_root``, parse every ``.py`` file, and return all matches.

    Pure and DB-independent: does not touch ``file_tasks`` or any session.
    """

    targets = [TargetSymbol.from_changelog_event(e) for e in changelog_events]
    if not targets:
        return []

    matches: list[Match] = []
    for path in sorted(repo_root.rglob("*.py")):
        rel = path.relative_to(repo_root)
        if should_skip_path(rel):
            continue
        source = path.read_bytes()
        matches.extend(
            find_matches_in_file(rel.as_posix(), source, targets)
        )
    return matches


def persist_matches(session, run_id: uuid.UUID, matches: list[Match]) -> int:
    """Upsert ``matches`` into ``file_tasks``, idempotently.

    Relies on ``uq_file_tasks_run_path_symbol_line`` (run_id, file_path,
    matched_symbol, line_start) for uniqueness. Checks for an existing row
    before inserting rather than relying on a DB-level ON CONFLICT clause,
    so this works identically across the SQLite substitute used in tests
    and the real Postgres backend.

    Returns the number of newly-inserted rows (0 on a full re-scan of an
    already-persisted run).
    """

    # Imported lazily so the pure matching half above has no hard
    # dependency on the ORM/DB layer at import time.
    from db.models import FileTask

    inserted = 0
    for m in matches:
        existing = (
            session.query(FileTask)
            .filter_by(
                run_id=run_id,
                file_path=m.file_path,
                matched_symbol=m.matched_symbol,
                line_start=m.line_start,
            )
            .one_or_none()
        )
        if existing is not None:
            continue
        session.add(
            FileTask(
                run_id=run_id,
                file_path=m.file_path,
                status="pending",
                matched_symbol=m.matched_symbol,
                line_start=m.line_start,
                line_end=m.line_end,
                confidence_score=m.confidence_score,
                changelog_event_id=m.changelog_event_id,
            )
        )
        inserted += 1
    session.flush()
    return inserted
