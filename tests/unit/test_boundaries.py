"""Architecture invariants, enforced rather than requested.

These tests exist so that CLAUDE.md's invariants cannot decay into comments
nobody reads. Each one fails the suite the moment the boundary it guards is
crossed, which is cheaper than discovering it at integration.
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
CORE = ROOT / "core"
APP = ROOT / "app"

FORBIDDEN_IN_CORE = {"sqlalchemy", "fastapi", "alembic", "pydantic", "app", "starlette", "jinja2"}


def _modules(package: Path) -> list[Path]:
    return sorted(p for p in package.rglob("*.py") if "__pycache__" not in p.parts)


def _imported_roots(path: Path) -> set[str]:
    tree = ast.parse(path.read_text())
    roots: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            roots.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            roots.add(node.module.split(".")[0])
    return roots


def test_core_imports_stdlib_only() -> None:
    """CLAUDE.md invariant 2. The brief requires comparison logic testable
    without a database and without a browser; an import is how that decays."""
    allowed = set(sys.stdlib_module_names) | {"core"}
    offenders: dict[str, set[str]] = {}
    for module in _modules(CORE):
        bad = _imported_roots(module) - allowed
        if bad:
            offenders[str(module.relative_to(ROOT))] = bad
    assert not offenders, f"core/ must import stdlib only, found: {offenders}"


def test_core_never_imports_infrastructure() -> None:
    for module in _modules(CORE):
        bad = _imported_roots(module) & FORBIDDEN_IN_CORE
        assert not bad, f"{module.relative_to(ROOT)} imports {bad}"


def test_no_create_all_outside_tests() -> None:
    """CLAUDE.md invariant 5. The schema is a deliverable; it comes from Alembic."""
    offenders = [
        str(p.relative_to(ROOT))
        for p in [*_modules(CORE), *_modules(APP)]
        if "create_all" in p.read_text()
    ]
    assert not offenders, f"create_all belongs only in test fixtures, found in {offenders}"


def test_no_dialect_branching() -> None:
    """TR-704. The database is chosen by URL; no code path knows which one it got."""
    offenders = []
    for path in [*_modules(CORE), *_modules(APP)]:
        text = path.read_text()
        for marker in ("dialect.name ==", "dialect.name in", "is_sqlite", "is_postgres"):
            if marker in text:
                offenders.append(f"{path.relative_to(ROOT)}: {marker}")
    assert not offenders, f"no code may branch on database backend: {offenders}"


def test_records_are_never_mutated() -> None:
    """CLAUDE.md invariant 3. A correction writes new rows; it never edits old ones."""
    forbidden = ("delete(Record", "update(Record", "Record).delete", "Record).update")
    offenders = []
    for path in _modules(APP / "services") if (APP / "services").exists() else []:
        text = path.read_text()
        offenders += [f"{path.relative_to(ROOT)}: {m}" for m in forbidden if m in text]
    assert not offenders, f"record rows are immutable: {offenders}"


def test_core_has_no_io() -> None:
    """A pure function that opens a file is not a pure function."""
    banned_calls = {"open", "print", "input"}
    offenders = []
    for path in _modules(CORE):
        for node in ast.walk(ast.parse(path.read_text())):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id in banned_calls
            ):
                offenders.append(f"{path.relative_to(ROOT)}: {node.func.id}()")
    assert not offenders, f"core/ performs no IO: {offenders}"
