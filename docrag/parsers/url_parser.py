"""URL / HTML parser.

Ingests a web page (or a local HTML file) into the same Document IR as PDF and
DOCX. The pipeline is the one from ``link2rag``, ported to permissive deps:

    URL ──fetch──▶ raw HTML ──html_to_md──▶ Markdown ──md_to_ir──▶ Document IR

The HTML→Markdown step uses :mod:`docrag.parsers.html_to_md`, which replaces
the GPL-licensed ``html2text`` with the MIT-licensed ``markdownify``. Optional
dynamic rendering (accordion expansion, scroll-based hydration) shells out to
the ``agent-browser`` CLI and is silently skipped when that tool is absent.

For tests, pass a local ``.html`` file path (or a ``file://`` URL) so no
network access is needed; the parser reads the file directly.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable

from ..ids import slugify
from ..ir import Document
from ..normalizer import Normalizer
from .base import BaseParser
from .html_to_md import DEFAULT_UA, fetch_html, html_to_markdown, render_dynamic_markdown
from .md_to_ir import markdown_to_document

Fetcher = Callable[[str, int, str], tuple[str, str]]


class UrlParser(BaseParser):
    """Parse a URL or local HTML file into a :class:`Document`."""

    file_type = "url"

    def __init__(
        self,
        normalizer: Normalizer | None = None,
        *,
        selector: str | None = None,
        strip_forms: bool = False,
        ignore_images: bool = False,
        render: bool = False,
        dynamic: bool = False,
        exclude_blogs: bool = False,
        exclude_products: bool = False,
        exclude_regexes: list[str] | None = None,
        timeout: int = 60,
        ua: str = DEFAULT_UA,
        fetcher: Fetcher | None = None,
    ) -> None:
        super().__init__(normalizer)
        self.selector = selector
        self.strip_forms = strip_forms
        self.ignore_images = ignore_images
        self.render = render
        self.dynamic = dynamic
        self.exclude_blogs = exclude_blogs
        self.exclude_products = exclude_products
        self.exclude_regexes = exclude_regexes
        self.timeout = timeout
        self.ua = ua
        self.fetcher = fetcher

    # -- public ----------------------------------------------------------- #
    def parse(self, path: str | Path, *, doc_id: str | None = None) -> Document:
        source, html, final_url = self._load(path)
        md, meta = html_to_markdown(
            html, final_url,
            selector=self.selector,
            strip_forms=self.strip_forms,
            ignore_images=self.ignore_images,
            exclude_regexes=self.exclude_regexes,
            exclude_blogs=self.exclude_blogs,
            exclude_products=self.exclude_products,
        )
        method = "static HTML"
        if self.render or self.dynamic:
            md, method = render_dynamic_markdown(
                final_url, md,
                render=self.render, dynamic=self.dynamic,
            )
        did = doc_id or slugify(Path(final_url).stem) or slugify(final_url) or "url"
        doc = markdown_to_document(
            md, doc_id=did, source_file=final_url,
            normalizer=self.normalizer, file_type=self.file_type,
        )
        doc.metadata.update(meta)
        doc.metadata["method"] = method
        doc.metadata["source_url"] = final_url
        doc.metadata["file_name"] = Path(final_url).name or final_url
        doc.metadata["file_type"] = self.file_type
        return doc

    # -- internals -------------------------------------------------------- #
    def _load(self, path: str | Path) -> tuple[str, str, str]:
        """Return ``(local_path_or_empty, html, final_url)``."""
        s = str(path)
        # Local file: a path that exists, or a file:// URL.
        if s.startswith("file://"):
            local = s[len("file://"):]
            return local, Path(local).read_text(encoding="utf-8", errors="replace"), s
        p = Path(s)
        if p.exists() and p.is_file():
            return s, p.read_text(encoding="utf-8", errors="replace"), s
        # Otherwise treat as a remote URL.
        if self.fetcher is not None:
            html, final_url = self.fetcher(s, self.timeout, self.ua)
        else:
            html, final_url = fetch_html(s, self.timeout, self.ua)
        return "", html, final_url
