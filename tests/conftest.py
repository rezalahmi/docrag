"""Shared pytest fixtures for the docrag test-suite."""
from __future__ import annotations

from pathlib import Path

import pytest

PKG_ROOT = Path(__file__).resolve().parent.parent
FIX_DIR = PKG_ROOT / "fixtures"


@pytest.fixture(scope="session")
def fixtures_dir() -> Path:
    return FIX_DIR


@pytest.fixture(scope="session")
def contract_docx(fixtures_dir: Path) -> Path:
    p = fixtures_dir / "contract.docx"
    if not p.exists():
        pytest.skip(f"fixture {p} not built; run scripts/build_fixtures.py")
    return p


@pytest.fixture(scope="session")
def financial_pdf(fixtures_dir: Path) -> Path:
    p = fixtures_dir / "financial_report.pdf"
    if not p.exists():
        pytest.skip(f"fixture {p} not built; run scripts/build_fixtures.py")
    return p
