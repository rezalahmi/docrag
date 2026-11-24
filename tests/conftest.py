"""Shared pytest fixtures for the docrag test-suite."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

PKG_ROOT = Path(__file__).resolve().parent.parent
FIX_DIR = PKG_ROOT / "fixtures"
sys.path.insert(0, str(PKG_ROOT))
sys.path.insert(0, str(PKG_ROOT / "scripts"))


def _ensure_fixtures() -> None:
    """Build any missing fixtures so the suite is self-contained.

    Best-effort: if the build toolchain (LibreOffice for PDF recalc / xlsx
    formula recalc) is absent, fixtures that need it simply will not exist and
    the tests depending on them skip.
    """
    needed = ["contract.docx", "financial_report.pdf",
              "نمایندگی‌ها.xlsx", "صورت_مالی.xlsx", "sample_page.html"]
    if all((FIX_DIR / n).exists() for n in needed):
        return
    try:
        import build_fixtures
        build_fixtures.main()
    except Exception as e:  # pragma: no cover
        print(f"fixture build skipped: {e}", file=sys.stderr)


@pytest.fixture(scope="session", autouse=True)
def _build_fixtures():
    _ensure_fixtures()
    yield


@pytest.fixture(scope="session")
def fixtures_dir() -> Path:
    return FIX_DIR


def _fixture(name: str):
    @pytest.fixture(scope="session")
    def _f(fixtures_dir: Path) -> Path:
        p = fixtures_dir / name
        if not p.exists():
            pytest.skip(f"fixture {p} not built; run scripts/build_fixtures.py")
        return p
    return _f


contract_docx = _fixture("contract.docx")
financial_pdf = _fixture("financial_report.pdf")
agencies_xlsx = _fixture("نمایندگی‌ها.xlsx")
financial_xlsx = _fixture("صورت_مالی.xlsx")
sample_html = _fixture("sample_page.html")


@pytest.fixture(scope="session")
def pipeline():
    from docrag import Pipeline
    return Pipeline()


@pytest.fixture(scope="session")
def agencies_chunks(pipeline, agencies_xlsx):
    return pipeline.run(agencies_xlsx).chunks


@pytest.fixture(scope="session")
def financial_chunks(pipeline, financial_xlsx):
    return pipeline.run(financial_xlsx).chunks


@pytest.fixture(scope="session")
def html_chunks(pipeline, sample_html):
    return pipeline.run(sample_html).chunks


@pytest.fixture(scope="session")
def combined_retriever(pipeline, agencies_xlsx, financial_xlsx, sample_html):
    """Retriever over chunks from all three Excel/URL fixtures."""
    from docrag import Retriever
    chunks = []
    for p in (agencies_xlsx, financial_xlsx, sample_html):
        chunks.extend(pipeline.run(p).chunks)
    return Retriever(chunks)
