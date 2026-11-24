"""Convert docrag chunks.json to markdown for visual review / RAG feed."""
import argparse
import json
import re
from pathlib import Path
from collections import defaultdict


SHEET_KEYS = ("sheet", "sheet_name", "worksheet", "ws")
TABLE_KEYS = ("table", "table_name", "tbl")
FILE_KEYS = ("filename", "source", "input", "file", "doc", "document")


def load_chunks(path):
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    if isinstance(data, list):
        return data, {}
    if isinstance(data, dict):
        if "chunks" in data and isinstance(data["chunks"], list):
            meta = {k: v for k, v in data.items() if k != "chunks"}
            return data["chunks"], meta
        return data, {}
    raise ValueError(f"Unexpected top-level type: {type(data)}")


def first_nonempty(d, keys, default=""):
    if not d:
        return default
    for k in keys:
        v = d.get(k)
        if v:
            return v
    return default


def gmeta(chunk, keys, default=""):
    return first_nonempty(chunk.get("metadata") or {}, keys, default)


def guess_filename(chunks, meta):
    fn = first_nonempty(meta, FILE_KEYS)
    if fn:
        return Path(str(fn)).name
    for c in chunks:
        cid = c.get("id", "") or ""
        m = re.match(r"^(.+?)::chunk::", cid)
        if m:
            return m.group(1)
    return "document"


def render(chunks, meta, out_path, limit=None):
    if limit:
        chunks = chunks[:limit]
    source = guess_filename(chunks, meta)
    lines = [
        f"# {source}",
        "",
        f"_{len(chunks)} chunks_",
        "",
    ]

    groups = defaultdict(lambda: defaultdict(list))
    for c in chunks:
        sheet = gmeta(c, SHEET_KEYS, "(no sheet)")
        table = gmeta(c, TABLE_KEYS, "(no table)")
        groups[sheet][table].append(c)

    multi_sheet = len(groups) > 1
    multi_table = sum(len(t) for t in groups.values()) > 1

    for sheet, tables in groups.items():
        if multi_sheet:
            lines += [f"## Sheet: {sheet}", ""]
        for table, cks in tables.items():
            if multi_table:
                lines += [f"### Table: {table}", ""]
            for c in cks:
                cid = c.get("id", "")
                ctype = c.get("type", "")
                text = (c.get("text") or "").rstrip()
                row = gmeta(c, ("row", "row_index", "row_num"), "")
                rng = gmeta(c, ("range", "cell_range", "address"), "")
                header = f"#### {cid}"
                extras = []
                if ctype: extras.append(f"type={ctype}")
                if row: extras.append(f"row={row}")
                if rng: extras.append(f"range={rng}")
                if extras:
                    header += "  · " + " · ".join(extras)
                lines += [header, ""]
                if text:
                    lines += [text, ""]
                lines += ["---", ""]

    out_path.write_text("\n".join(lines), encoding="utf-8")
    print(f"Wrote {out_path} ({len(lines)} lines, {len(chunks)} chunks)")


def main():
    p = argparse.ArgumentParser(description="Convert docrag chunks.json to markdown")
    p.add_argument("chunks_json", help="Path to chunks.json")
    p.add_argument("output", nargs="?", help="Output .md path (default: same name with .md)")
    p.add_argument("--limit", type=int, default=None, help="Only convert first N chunks (preview)")
    args = p.parse_args()

    src = Path(args.chunks_json)
    if not src.exists():
        p.error(f"File not found: {src}")
    dst = Path(args.output) if args.output else src.with_suffix(".md")

    chunks, meta = load_chunks(src)
    print(f"Loaded {len(chunks)} chunks from {src}")
    render(chunks, meta, dst, limit=args.limit)


if __name__ == "__main__":
    main()
