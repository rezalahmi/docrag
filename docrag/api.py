"""FastAPI REST interface.

Exposes the pipeline over HTTP so Hoshi-Bot (or any service) can call it
without shelling out to the CLI. The ``api`` extra (``fastapi``, ``uvicorn``)
is optional; importing this module fails gracefully if FastAPI is missing.

Endpoints
---------
* ``GET  /healthz``        — liveness probe.
* ``POST /process``        — upload a PDF/DOCX, run the full pipeline, return
  IR + chunks + a validation summary. Optionally ``?save=true`` writes
  artifacts to ``out_dir`` and returns their paths.
* ``POST /chunk``          — send an IR JSON body, get chunks JSON back.
* ``POST /normalize``      — normalise a text body.
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path
from typing import Any

from fastapi import FastAPI, File, HTTPException, UploadFile, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from .chunker import SemanticChunker
from .ir import Document
from .normalizer import Normalizer, NormalizerConfig
from .pipeline import Pipeline
from .validator import Validator

app = FastAPI(
    title="docrag",
    version="0.2.0",
    description="Persian document preprocessing pipeline for RAG.",
)


class ChunkRequest(BaseModel):
    document: dict[str, Any]
    max_tokens: int = 400
    overlap_sentences: int = 0


class NormalizeRequest(BaseModel):
    text: str
    digits: str = "persian"


@app.get("/healthz")
def healthz() -> dict[str, str]:
    return {"status": "ok", "version": app.version}


@app.post("/process")
async def process(
    file: UploadFile = File(...),
    max_tokens: int = Query(400, ge=64, le=8000),
    digits: str = Query("persian"),
    overlap: int = Query(0, ge=0, le=5),
    save: bool = Query(False),
    out_dir: str | None = Query(None),
) -> JSONResponse:
    """Run the full pipeline on an uploaded file."""
    suffix = Path(file.filename or "").suffix.lower()
    if suffix not in {".pdf", ".docx"}:
        raise HTTPException(
            status_code=415,
            detail=f"Unsupported file type {suffix!r}; expected .pdf or .docx",
        )
    cfg = NormalizerConfig(digits=digits)
    pipe = Pipeline(max_tokens=max_tokens, normalizer_config=cfg, overlap_sentences=overlap)

    with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
        tmp.write(await file.read())
        tmp_path = Path(tmp.name)

    try:
        result = pipe.run(tmp_path)
        # Restore the original filename on the document so downstream ids
        # reflect the user's file rather than the temp name.
        result.document.source_file = file.filename or tmp_path.name
        for el in result.document.elements:
            el.source.file = result.document.source_file
        payload: dict[str, Any] = {
            "document": result.document.to_dict(),
            "chunks": [c.to_dict() for c in result.chunks],
            "report": result.report.to_dict() if result.report else {},
        }
        if save:
            target = Path(out_dir) if out_dir else Path(tempfile.mkdtemp(prefix="docrag_"))
            pipe.write_outputs(result, target)
            payload["outputs"] = {k: str(v) for k, v in result.outputs.items()}
        return JSONResponse(payload)
    finally:
        tmp_path.unlink(missing_ok=True)


@app.post("/chunk")
def chunk(req: ChunkRequest) -> JSONResponse:
    """Re-derive chunks from an IR JSON document (no re-parsing)."""
    try:
        doc = Document.from_dict(req.document)
    except Exception as e:
        raise HTTPException(status_code=422, detail=f"Invalid IR document: {e}")
    chunker = SemanticChunker(
        max_tokens=req.max_tokens, overlap_sentences=req.overlap_sentences,
    )
    chunks = chunker.chunk(doc)
    report = Validator(max_tokens=req.max_tokens).validate(doc, chunks)
    return JSONResponse({
        "chunks": [c.to_dict() for c in chunks],
        "report": report.to_dict(),
    })


@app.post("/normalize")
def normalize(req: NormalizeRequest) -> dict[str, str]:
    if req.digits not in ("persian", "latin", "keep"):
        raise HTTPException(status_code=422, detail="digits must be persian|latin|keep")
    out = Normalizer(NormalizerConfig(digits=req.digits)).normalize(req.text)
    return {"normalized": out}
