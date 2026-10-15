"""docs/requirements_audit.md names the evidence for every requirement, and docs/production_readiness.md names what exists today. This test
keeps both honest: every file path and every test name they cite must exist, so renaming a test or deleting a file breaks the build instead
of silently making a document false."""

from __future__ import annotations

import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
AUDIT = REPO / "docs" / "requirements_audit.md"
READINESS = REPO / "docs" / "production_readiness.md"
DOCS = (AUDIT, READINESS)
TICKS = re.compile(r"`([^`\n]+)`")
PATHLIKE = re.compile(r"^[A-Za-z0-9_.\-/]+\.(?:py|md|ts|tsx|yml|yaml|json|lock|toml|pdf|example|gitignore|sh)$")


def tokens(doc: Path = AUDIT) -> list[str]:
    return TICKS.findall(doc.read_text(encoding="utf-8"))


def test_the_audit_cites_a_reasonable_amount_of_evidence():
    found = tokens()
    paths = [t for t in found if PATHLIKE.match(t) or t in {".gitignore", ".env.example"}]
    names = [t for t in found if t.startswith("test_")]
    assert len(paths) >= 60 and len(names) >= 20, (len(paths), len(names))


def test_every_file_the_documents_cite_exists():
    missing = []
    for doc in DOCS:
        for token in tokens(doc):
            if PATHLIKE.match(token) or token in {".gitignore", ".env.example"}:
                if "/" in token and not (REPO / token).exists():
                    missing.append(f"{doc.name}: {token}")
                elif "/" not in token and not any(REPO.glob(token)) and not (REPO / token).exists():
                    missing.append(f"{doc.name}: {token}")
    assert not missing, f"cited but missing: {sorted(set(missing))}"


def test_every_test_the_documents_cite_exists():
    sources = "\n".join(p.read_text(encoding="utf-8") for p in (REPO / "backend" / "tests").glob("*.py"))
    missing = [f"{doc.name}: {t}" for doc in DOCS for t in tokens(doc) if t.startswith("test_") and not re.search(rf"def {re.escape(t)}\b", sources)]
    assert not missing, f"cited but not found: {sorted(set(missing))}"


def test_the_production_readiness_document_has_its_tiers_and_cites_real_evidence():
    text = READINESS.read_text(encoding="utf-8")
    for heading in ("## Tier 1", "## Tier 2", "## Tier 3", "## Suggested order", "## A definition of \"ready\" that can be checked"):
        assert heading in text, heading
    assert "not production-ready" in text
    assert len([t for t in tokens(READINESS) if PATHLIKE.match(t)]) >= 15


def test_every_requirement_row_has_a_known_status():
    allowed = ("Met", "Met, with a documented deviation", "Partly", "Deferred by the specification")
    rows = [line for line in AUDIT.read_text(encoding="utf-8").splitlines() if line.startswith("| ") and "---" not in line]
    body = [r.split("|")[1:-1] for r in rows]
    statused = [cells for cells in body if len(cells) == 3 and cells[1].strip() in allowed]
    assert len(statused) >= 60  # the table rows of sections 1 to 11; deliverables have their own statuses
    for cells in body:
        if len(cells) == 3 and cells[1].strip() not in ("Status", "Deliverable") and cells[0].strip() not in ("Requirement", "Deliverable"):
            assert cells[1].strip() in allowed, cells
