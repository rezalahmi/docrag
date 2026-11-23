"""End-to-end demo: run the full pipeline on the bundled fixtures.

Run::

    python examples/demo.py

Prints a per-file summary and writes all artifacts under output/.
"""
from __future__ import annotations

from collections import Counter
from pathlib import Path

from docrag import Pipeline

HERE = Path(__file__).resolve().parent
PKG = HERE.parent
FIX = PKG / "fixtures"
OUT = HERE / "output"


def main() -> None:
    OUT.mkdir(exist_ok=True)
    pipe = Pipeline(max_tokens=400)
    files = [p for p in FIX.iterdir() if p.suffix.lower() in {".pdf", ".docx"}]
    if not files:
        raise SystemExit("No fixtures found — run scripts/build_fixtures.py first.")
    for f in sorted(files):
        result = pipe.process(f, OUT)
        d = result.document
        counts = Counter(e.kind.value for e in d.elements)
        print(f"\n=== {d.source_file} ({d.file_type}) ===")
        print(f"  elements : {len(d)}  {dict(counts)}")
        print(f"  sections : {len(d.sections)}")
        print(f"  chunks   : {len(result.chunks)}")
        chunk_types = Counter(c.type for c in result.chunks)
        print(f"  chunk types: {dict(chunk_types)}")
        m = result.report.metrics
        if "chunk_coverage" in m:
            print(f"  coverage : {m['chunk_coverage']:.0%}")
            print(f"  tokens   : min {m['chunk_tokens_min']} / "
                  f"mean {m['chunk_tokens_mean']} / max {m['chunk_tokens_max']}")
        print(f"  validation: {'OK' if result.ok else 'FAIL'} "
              f"({result.report.error_count} errors, {result.report.warning_count} warnings)")
        for kind in ("ir", "html", "chunks", "report"):
            print(f"    {kind:7}: {result.outputs[kind]}")
    print(f"\nAll artifacts written under: {OUT}")


if __name__ == "__main__":
    main()
