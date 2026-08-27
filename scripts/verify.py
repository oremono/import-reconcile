"""The oracle. Answers "is it done?" with a number rather than a judgement.

Done is: every stage green, TR 82/82, AC 12/12.

The interesting stage is 6. ``docs/REQUIREMENTS.md`` names, for each of its 82
requirements, the test that proves it. This parses that column, runs the suite,
and reports which requirements have a test that actually ran and passed. A
requirement whose test does not exist reports unverified, so it cannot be
quietly skipped.

Usage:  make verify   /   uv run python scripts/verify.py [--fast]
"""

from __future__ import annotations

import re
import subprocess
import sys
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
REQUIREMENTS = ROOT / "docs" / "REQUIREMENTS.md"
JUNIT = ROOT / ".verify" / "junit.xml"
COVERAGE_FLOOR = 90

GREEN, RED, YELLOW, DIM, RESET = "\033[32m", "\033[31m", "\033[33m", "\033[2m", "\033[0m"
TICK, CROSS, DOT = "OK  ", "FAIL", "--  "


@dataclass
class Stage:
    name: str
    ok: bool
    detail: str = ""
    skipped: bool = False


@dataclass
class Requirement:
    id: str
    tests: list[str] = field(default_factory=list)
    manual: list[str] = field(default_factory=list)

    @property
    def automated(self) -> bool:
        return bool(self.tests)


# ---------------------------------------------------------------------------
# Parsing the requirements document
# ---------------------------------------------------------------------------

TEST_REF = re.compile(r"`(test_[A-Za-z0-9_]+\.py(?:::[A-Za-z0-9_]+)?)`")
ROW = re.compile(r"^\|\s*(TR-\d{3})\s*\|(.+)$")


def parse_requirements() -> list[Requirement]:
    out: list[Requirement] = []
    for line in REQUIREMENTS.read_text(encoding="utf-8").splitlines():
        m = ROW.match(line)
        if not m:
            continue
        cells = [c.strip() for c in m.group(2).split("|")]
        verified_by = cells[2] if len(cells) >= 3 else ""
        req = Requirement(id=m.group(1))
        req.tests = TEST_REF.findall(verified_by)
        if not req.tests and verified_by:
            req.manual = [verified_by]
        out.append(req)
    return out


AC_MAP = re.compile(r"(AC\d+)\s*\(([^)]*)\)")


def parse_acceptance() -> dict[str, list[str]]:
    text = REQUIREMENTS.read_text(encoding="utf-8")
    return {ac: re.findall(r"TR-\d{3}", trs) for ac, trs in AC_MAP.findall(text)}


# ---------------------------------------------------------------------------
# Running things
# ---------------------------------------------------------------------------


def run(cmd: list[str], **kw) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True, **kw)


def passed_tests() -> set[str]:
    """Return {"test_file.py::test_name"} for every test that ran and passed."""
    if not JUNIT.exists():
        return set()
    root = ET.parse(JUNIT).getroot()
    out: set[str] = set()
    for case in root.iter("testcase"):
        if any(case.find(tag) is not None for tag in ("failure", "error", "skipped")):
            continue
        classname, name = case.get("classname", ""), case.get("name", "")
        module = classname.split(".")[-1] if classname else ""
        if module:
            out.add(f"{module}.py::{name}")
    return out


def satisfied(ref: str, passes: set[str]) -> bool:
    if "::" in ref:
        return ref in passes
    return any(p.startswith(f"{ref}::") for p in passes)


# ---------------------------------------------------------------------------
# Stages
# ---------------------------------------------------------------------------


def stage_lint() -> list[Stage]:
    a = run(["uv", "run", "ruff", "check", "."])
    b = run(["uv", "run", "ruff", "format", "--check", "."])
    return [
        Stage(
            "1  lint", a.returncode == 0, a.stdout.strip().splitlines()[-1] if a.returncode else ""
        ),
        Stage("   format", b.returncode == 0, "run `make fmt`" if b.returncode else ""),
    ]


def stage_types() -> Stage:
    r = run(["uv", "run", "mypy", "core", "app"])
    last = [ln for ln in r.stdout.strip().splitlines() if ln][-1] if r.stdout.strip() else ""
    return Stage("2  types", r.returncode == 0, last)


def stage_unit_isolation() -> Stage:
    """tests/unit must not reach for a database or the app. The brief's requirement."""
    banned = {"sqlalchemy", "fastapi", "app"}
    offenders = []
    for path in sorted((ROOT / "tests" / "unit").glob("*.py")):
        text = path.read_text()
        for token in banned:
            if re.search(rf"^\s*(from|import)\s+{token}\b", text, re.M):
                offenders.append(f"{path.name} imports {token}")
    return Stage("3  unit isolation", not offenders, "; ".join(offenders))


def stage_tests() -> tuple[Stage, Stage, Stage]:
    """Run both suites once, then read pass/fail from junit and coverage from stdout.

    pytest exits non-zero for a coverage shortfall as well as for a failing test,
    so the two are read separately rather than inferred from the exit code.
    """
    JUNIT.parent.mkdir(exist_ok=True)
    unit = run(["uv", "run", "pytest", "tests/unit", "-q"])
    full = run(
        [
            "uv",
            "run",
            "pytest",
            f"--junitxml={JUNIT}",
            "--cov=core",
            "--cov-report=term",
            "-q",
        ]
    )

    failures, errors, total = 0, 0, 0
    if JUNIT.exists():
        root = ET.parse(JUNIT).getroot()
        for suite in root.iter("testsuite"):
            failures += int(suite.get("failures", 0))
            errors += int(suite.get("errors", 0))
            total += int(suite.get("tests", 0))

    cov_line = next((ln for ln in full.stdout.splitlines() if ln.startswith("TOTAL")), "")
    match = re.search(r"(\d+)%\s*$", cov_line)
    coverage = int(match.group(1)) if match else 0

    suite_detail = f"{total - failures - errors}/{total} passed"
    if failures or errors:
        suite_detail += f" ({failures} failed, {errors} errored)"

    return (
        Stage("   unit suite", unit.returncode == 0, tail_of(unit)),
        Stage("4  full suite", total > 0 and not failures and not errors, suite_detail),
        Stage(
            f"5  coverage >= {COVERAGE_FLOOR}%",
            coverage >= COVERAGE_FLOOR,
            f"core {coverage}%",
        ),
    )


def tail_of(proc: subprocess.CompletedProcess[str]) -> str:
    lines = [ln for ln in proc.stdout.strip().splitlines() if ln]
    return lines[-1] if lines else ""


def stage_routes() -> Stage:
    main = ROOT / "app" / "main.py"
    if not main.exists():
        return Stage("8  route smoke", False, "app/main.py not built yet", skipped=True)
    r = run(["uv", "run", "python", "-c", "from app.main import app; print(len(app.routes))"])
    return Stage("8  route smoke", r.returncode == 0, r.stdout.strip() or tail_of(r))


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------


def main() -> int:
    stages: list[Stage] = []
    stages += stage_lint()
    stages.append(stage_types())
    stages.append(stage_unit_isolation())
    stages += list(stage_tests())

    passes = passed_tests()
    reqs = parse_requirements()
    verified = {r.id for r in reqs if r.automated and all(satisfied(t, passes) for t in r.tests)}
    manual_only = {r.id for r in reqs if not r.automated}
    automated = [r for r in reqs if r.automated]
    missing = sorted({r.id for r in automated} - verified)

    acs = parse_acceptance()
    ac_ok = {ac for ac, trs in acs.items() if trs and all(t in verified for t in trs)}
    ac_missing = sorted(set(acs) - ac_ok, key=lambda a: int(a[2:]))

    stages.append(
        Stage(
            f"6  requirements  {len(verified)}/{len(automated)}",
            not missing,
            f"unverified: {', '.join(missing[:8])}{' ...' if len(missing) > 8 else ''}",
        )
    )
    stages.append(
        Stage(
            f"7  acceptance    {len(ac_ok)}/{len(acs)}",
            not ac_missing,
            f"unproven: {', '.join(ac_missing)}",
        )
    )
    stages.append(stage_routes())

    print()
    for s in stages:
        mark = DOT if s.skipped else (TICK if s.ok else CROSS)
        colour = YELLOW if s.skipped else (GREEN if s.ok else RED)
        print(f"  {colour}{mark}{RESET}  {s.name:28s} {DIM}{s.detail[:90]}{RESET}")

    hard = [s for s in stages if not s.ok and not s.skipped]
    soft = [s for s in stages if s.skipped]
    print()
    manual_note = f"{DIM}({len(manual_only)} manual){RESET}"
    print(f"  TR verified   {len(verified)} / {len(automated)}   {manual_note}")
    print(f"  AC verified   {len(ac_ok)} / {len(acs)}")
    print()
    if hard:
        print(
            f"  {RED}not done{RESET} - {len(hard)} stage(s) failing"
            + (f", {len(soft)} pending" if soft else "")
        )
        return 1
    if soft:
        print(f"  {YELLOW}pending{RESET} - {len(soft)} stage(s) not built yet")
        return 1
    print(f"  {GREEN}done{RESET}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
