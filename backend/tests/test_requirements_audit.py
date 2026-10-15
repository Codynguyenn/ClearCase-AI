"""docs/requirements_audit.md names the evidence for every requirement. This test keeps it honest: every file path and every test
name it cites must exist, so renaming a test or deleting a file breaks the build instead of silently making the audit false."""

from __future__ import annotations

import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
AUDIT = REPO / "docs" / "requirements_audit.md"
TICKS = re.compile(r"`([^`\n]+)`")
PATHLIKE = re.compile(r"^[A-Za-z0-9_.\-/]+\.(?:py|md|ts|tsx|yml|yaml|json|lock|toml|pdf|example|gitignore|sh)$")


def tokens() -> list[str]:
    return TICKS.findall(AUDIT.read_text(encoding="utf-8"))


def test_the_audit_cites_a_reasonable_amount_of_evidence():
    found = tokens()
    paths = [t for t in found if PATHLIKE.match(t) or t in {".gitignore", ".env.example"}]
    names = [t for t in found if t.startswith("test_")]
    assert len(paths) >= 60 and len(names) >= 20, (len(paths), len(names))


def test_every_file_the_audit_cites_exists():
    missing = []
    for token in tokens():
        if PATHLIKE.match(token) or token in {".gitignore", ".env.example"}:
            if "/" in token and not (REPO / token).exists():
                missing.append(token)
            elif "/" not in token and not any(REPO.glob(token)) and not (REPO / token).exists():
                missing.append(token)
    assert not missing, f"cited but missing: {sorted(set(missing))}"


def test_every_test_the_audit_cites_exists():
    sources = "\n".join(p.read_text(encoding="utf-8") for p in (REPO / "backend" / "tests").glob("*.py"))
    missing = [t for t in tokens() if t.startswith("test_") and not re.search(rf"def {re.escape(t)}\b", sources)]
    assert not missing, f"cited but not found: {sorted(set(missing))}"


def test_every_requirement_row_has_a_known_status():
    allowed = ("Met", "Met, with a documented deviation", "Partly", "Deferred by the specification")
    rows = [line for line in AUDIT.read_text(encoding="utf-8").splitlines() if line.startswith("| ") and "---" not in line]
    body = [r.split("|")[1:-1] for r in rows]
    statused = [cells for cells in body if len(cells) == 3 and cells[1].strip() in allowed]
    assert len(statused) >= 60  # the table rows of sections 1 to 11; deliverables have their own statuses
    for cells in body:
        if len(cells) == 3 and cells[1].strip() not in ("Status", "Deliverable") and cells[0].strip() not in ("Requirement", "Deliverable"):
            assert cells[1].strip() in allowed, cells
