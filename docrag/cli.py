"""Command-line interface (Click).

Usage examples::

    docrag process input/contract.docx --output output --max-tokens 400
    docrag process input/ --output output            # whole directory
    docrag parse input/manual.pdf --output ir/
    docrag chunk ir/contract.json --output chunks/contract.chunks.json
    docrag validate ir/contract.json --chunks chunks/contract.chunks.json
    docrag normalize --text "مي‌كند" --digits persian
    docrag serve --port 8080                          # REST API
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import click

from .chunker import SemanticChunker
from .ir import Document
from .normalizer import Normalizer, NormalizerConfig
from .pipeline import Pipeline
from .renderer import HtmlRenderer
from .validator import Validator


def _digits_option(fn):
    return click.option(
        "--digits",
        type=click.Choice(["persian", "latin", "keep"]),
        default="persian",
        help="Digit normalisation: persian (default), latin, keep.",
    )(fn)


@click.group(context_settings={"help_option_names": ["-h", "--help"]})
@click.version_option(package_name="docrag")
def cli() -> None:
    """docrag — Persian document preprocessing for RAG."""


@cli.command("process")
@click.argument("input_path", type=click.Path(exists=True, path_type=Path))
@click.option("--output", "-o", type=click.Path(path_type=Path), required=True,
              help="Output directory for IR/HTML/chunks/report.")
@click.option("--max-tokens", default=400, show_default=True, type=int)
@click.option("--overlap", default=0, show_default=True, type=int,
              help="Overlap in sentences between consecutive text chunks.")
@_digits_option
def process(input_path: Path, output: Path, max_tokens: int, overlap: int, digits: str) -> None:
    """Parse + chunk + render + validate a file (or a directory of files)."""
    cfg = NormalizerConfig(digits=digits)
    pipe = Pipeline(max_tokens=max_tokens, normalizer_config=cfg, overlap_sentences=overlap)
    if input_path.is_dir():
        results = pipe.process_dir(input_path, output)
    else:
        results = [pipe.process(input_path, output)]
    total_errors = 0
    for r in results:
        name = r.document.source_file
        n_chunks = len(r.chunks)
        ok = r.ok
        err = r.report.error_count if r.report else 0
        warn = r.report.warning_count if r.report else 0
        total_errors += err
        click.echo(
            f"{name}: {len(r.document)} elements → {n_chunks} chunks "
            f"| validation: {'OK' if ok else 'FAIL'} ({err} errors, {warn} warnings)"
        )
    if total_errors:
        click.echo(f"\n{total_errors} validation error(s) across {len(results)} file(s).", err=True)
        sys.exit(1)


@cli.command("parse")
@click.argument("input_path", type=click.Path(exists=True, path_type=Path))
@click.option("--output", "-o", type=click.Path(path_type=Path), required=True)
@_digits_option
def parse(input_path: Path, output: Path, digits: str) -> None:
    """Parse only: produce IR JSON + HTML preview (no chunks)."""
    cfg = NormalizerConfig(digits=digits)
    pipe = Pipeline(normalizer_config=cfg)
    result = pipe.run(input_path)
    pipe.write_outputs(result, output)
    # Remove the chunks/report written by write_outputs to keep "parse" lean.
    stem = result.outputs.get("chunks")
    if stem and stem.exists():
        stem.unlink()
    rep = result.outputs.get("report")
    if rep and rep.exists():
        rep.unlink()
    click.echo(f"IR:  {result.outputs['ir']}")
    click.echo(f"HTML:{result.outputs['html']}")
    click.echo(f"{len(result.document)} elements parsed.")


@cli.command("chunk")
@click.argument("ir_path", type=click.Path(exists=True, dir_okay=False, path_type=Path))
@click.option("--output", "-o", type=click.Path(path_type=Path), required=True)
@click.option("--max-tokens", default=400, show_default=True, type=int)
@click.option("--overlap", default=0, show_default=True, type=int)
def chunk(ir_path: Path, output: Path, max_tokens: int, overlap: int) -> None:
    """Chunk an existing IR JSON (re-derive chunks without re-parsing)."""
    data = json.loads(ir_path.read_text(encoding="utf-8"))
    doc = Document.from_dict(data)
    chunker = SemanticChunker(max_tokens=max_tokens, overlap_sentences=overlap)
    chunks = chunker.chunk(doc)
    output = Path(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps([c.to_dict() for c in chunks], ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    click.echo(f"{len(chunks)} chunks → {output}")


@cli.command("validate")
@click.argument("ir_path", type=click.Path(exists=True, dir_okay=False, path_type=Path))
@click.option("--chunks", "chunks_path", type=click.Path(exists=True, dir_okay=False, path_type=Path),
              default=None, help="Optional chunks JSON to validate alongside.")
@click.option("--max-tokens", default=400, show_default=True, type=int)
def validate(ir_path: Path, chunks_path: Path | None, max_tokens: int) -> None:
    """Validate an IR JSON (and optional chunks JSON)."""
    doc = Document.from_dict(json.loads(ir_path.read_text(encoding="utf-8")))
    chunks = None
    if chunks_path:
        from .chunker import Chunk
        raw = json.loads(chunks_path.read_text(encoding="utf-8"))
        chunks = [Chunk(**{**c, "source": _src(c.get("source"))}) for c in raw]
    report = Validator(max_tokens=max_tokens).validate(doc, chunks)
    click.echo(json.dumps(report.to_dict(), ensure_ascii=False, indent=2))
    sys.exit(0 if report.ok else 1)


@cli.command("normalize")
@click.option("--text", "-t", "text", default=None, help="Text to normalise.")
@click.option("--file", "file", type=click.Path(exists=True, dir_okay=False, path_type=Path),
              default=None, help="File whose contents to normalise.")
@_digits_option
def normalize_cmd(text: str | None, file: Path | None, digits: str) -> None:
    """Normalise Persian text (handy for quick checks / preprocessing queries)."""
    if file:
        text = file.read_text(encoding="utf-8")
    if text is None:
        text = sys.stdin.read()
    click.echo(Normalizer(NormalizerConfig(digits=digits)).normalize(text))


@cli.command("render")
@click.argument("ir_path", type=click.Path(exists=True, dir_okay=False, path_type=Path))
@click.option("--output", "-o", type=click.Path(path_type=Path), required=True)
def render(ir_path: Path, output: Path) -> None:
    """Render an IR JSON to HTML for visual review."""
    doc = Document.from_dict(json.loads(ir_path.read_text(encoding="utf-8")))
    HtmlRenderer().write_html(doc, output)
    # write_html returns a Path; emit it.
    click.echo(f"HTML → {output}")


@cli.command("serve")
@click.option("--host", default="127.0.0.1", show_default=True)
@click.option("--port", default=8000, show_default=True, type=int)
@click.option("--reload/--no-reload", default=False)
def serve(host: str, port: int, reload: bool) -> None:
    """Run the REST API (FastAPI). Requires the ``api`` extra."""
    try:
        import uvicorn
    except ImportError:
        raise click.ClickException(
            "uvicorn/fastapi not installed. Install with: pip install 'docrag[api]'"
        )
    uvicorn.run("docrag.api:app", host=host, port=port, reload=reload)


def _src(d):
    from .ir import SourceRef
    if not d:
        return None
    return SourceRef(
        file=d.get("file", ""),
        page=d.get("page"),
        bbox=d.get("bbox"),
        paragraph_index=d.get("paragraph_index"),
        page_width=d.get("page_width"),
        page_height=d.get("page_height"),
    )


def main() -> None:
    cli()


if __name__ == "__main__":
    main()
