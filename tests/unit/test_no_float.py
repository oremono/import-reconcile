"""No money value passes through a float. CLAUDE.md invariant 1.

Reconciliation is arithmetic about money. A float on this path is the exact
class of defect the application exists to detect in other people's systems, so
it is checked rather than trusted.
"""

from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
GUARDED = [ROOT / "core", ROOT / "app" / "services", ROOT / "app" / "db"]


def _modules() -> list[Path]:
    out: list[Path] = []
    for package in GUARDED:
        if package.exists():
            out += [p for p in package.rglob("*.py") if "__pycache__" not in p.parts]
    return sorted(out)


def test_no_float_calls() -> None:
    offenders = []
    for path in _modules():
        for node in ast.walk(ast.parse(path.read_text())):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "float"
            ):
                offenders.append(f"{path.relative_to(ROOT)}:{node.lineno}")
    assert not offenders, f"float() on the money path: {offenders}"


def test_no_float_literals() -> None:
    offenders = []
    for path in _modules():
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, ast.Constant) and isinstance(node.value, float):
                offenders.append(f"{path.relative_to(ROOT)}:{node.lineno} -> {node.value!r}")
    assert not offenders, f'float literals on the money path; use Decimal("..."): {offenders}'


def test_decimal_division_never_uses_true_divide_on_ints() -> None:
    """`a / b` on two ints yields a float. Guard the money modules against it."""
    offenders = []
    for path in _modules():
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div):
                operands = (node.left, node.right)
                if all(isinstance(o, ast.Constant) and isinstance(o.value, int) for o in operands):
                    offenders.append(f"{path.relative_to(ROOT)}:{node.lineno}")
    assert not offenders, f"integer division yields float: {offenders}"
