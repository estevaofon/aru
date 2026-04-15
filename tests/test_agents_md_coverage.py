"""Anti-drift guard: AGENTS.md must mention every top-level module under aru/.

Prevents the documentation from silently going stale when new modules land.
If a new module is genuinely internal and should not be documented, add its
stem to ALLOWLIST with a short justification comment.
"""

from __future__ import annotations

from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
ARU_DIR = REPO_ROOT / "aru"
AGENTS_MD = REPO_ROOT / "AGENTS.md"

ALLOWLIST: set[str] = set()


def _collect_modules() -> list[Path]:
    return [
        p
        for p in sorted(ARU_DIR.rglob("*.py"))
        if p.name != "__init__.py" and "__pycache__" not in p.parts
    ]


def test_agents_md_exists():
    assert AGENTS_MD.exists(), f"AGENTS.md missing at {AGENTS_MD}"


def test_every_module_is_mentioned():
    text = AGENTS_MD.read_text(encoding="utf-8")
    missing: list[str] = []
    for module in _collect_modules():
        stem = module.stem
        if stem in ALLOWLIST:
            continue
        filename = module.name
        rel = module.relative_to(ARU_DIR).as_posix()
        if filename in text or rel in text:
            continue
        missing.append(rel)

    if missing:
        pytest.fail(
            "AGENTS.md does not mention the following modules:\n  - "
            + "\n  - ".join(missing)
            + "\n\nAdd a one-liner for each new module, or append it to ALLOWLIST "
            "in tests/test_agents_md_coverage.py with a justification."
        )
