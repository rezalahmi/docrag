"""HTML → Markdown converter for URL ingestion.

This is a permissive-licensed port of the ``link2rag`` approach: fetch a page,
strip non-content DOM, de-duplicate the mobile/desktop duplicate blocks that
WordPress/Elementor and Next.js render, then convert to clean Markdown plus a
YAML front-matter block.

**License note.** The original ``link2rag`` used ``html2text`` for the HTML→
Markdown step, but ``html2text`` is **GPL-3.0** — a copyleft license that
would force any web service linking it to also publish its source. To keep
``docrag``'s dependency tree fully permissive (MIT/BSD/Apache only), this port
swaps ``html2text`` for ``markdownify`` (MIT). The de-duplication, filtering
and post-processing logic is preserved unchanged because it is what makes the
output retrieval-friendly.

Dependencies (all permissive):
* ``beautifulsoup4`` — MIT
* ``markdownify`` — MIT
* ``urllib`` — Python standard library

The optional dynamic rendering (accordion expansion, scroll-based hydration)
shells out to the ``agent-browser`` CLI when present; it is **not** a Python
dependency and is silently skipped when the tool is absent.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import re
import shutil
import subprocess
import sys
import urllib.parse
import urllib.request
from html import unescape
from typing import Iterable

try:
    from bs4 import BeautifulSoup, Comment
    from bs4.element import Tag
except ImportError:  # pragma: no cover
    sys.exit("خطا: bs4 نصب نیست. اجرا: pip install beautifulsoup4")

try:
    from markdownify import markdownify as _md_convert
except ImportError:  # pragma: no cover
    sys.exit("خطا: markdownify نصب نیست. اجرا: pip install markdownify")


# --------------------------------------------------------------------------- #
# 1. دریافت HTML
# --------------------------------------------------------------------------- #
DEFAULT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
)


def fetch_html(url: str, timeout: int = 60, ua: str = DEFAULT_UA) -> tuple[str, str]:
    """دانلود HTML خام و بازگرداندن (html, final_url)."""
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": ua,
            "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
            "Accept-Language": "fa,en;q=0.9",
        },
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read()
        final_url = resp.geturl()
        charset = resp.headers.get_content_charset() or "utf-8"
    try:
        html = raw.decode(charset, errors="replace")
    except (LookupError, TypeError):
        html = raw.decode("utf-8", errors="replace")
    return html, final_url


# --------------------------------------------------------------------------- #
# 2. پارس و پاک‌سازی DOM
# --------------------------------------------------------------------------- #
STRIP_TAGS = (
    "script", "style", "noscript", "link", "meta", "svg", "path",
    "iframe", "template", "canvas",
)


def parse_html(html: str, selector: str | None = None, strip_forms: bool = False) -> BeautifulSoup:
    """پارس HTML، حذف تگ‌های غیرمحتوایی، و در صورت وجود selector محدود کردن به آن."""
    soup = BeautifulSoup(html, "html.parser")
    tags_to_strip = list(STRIP_TAGS)
    if strip_forms:
        tags_to_strip += ["form", "input", "select", "button"]
    for tag in soup(tags_to_strip):
        tag.decompose()
    for c in soup.find_all(string=lambda s: isinstance(s, Comment)):
        c.extract()
    if selector:
        node = soup.select_one(selector)
        if node:
            new_soup = BeautifulSoup("<html><body></body></html>", "html.parser")
            new_soup.body.replace_with(node)
            return new_soup
        print(f"هشدار: selector «{selector}» چیزی پیدا نشد؛ کل body استفاده می‌شود.",
              file=sys.stderr)
    return soup


def dedup_duplicate_siblings(root: Tag, max_len: int = 80) -> int:
    """حذف siblingهای متوالی با متن یکسان (موبایل/دسکتاپ تکراری در Next.js)."""
    removed = 0
    for parent in root.find_all(True):
        children = [c for c in parent.children if isinstance(c, Tag)]
        i = 0
        while i < len(children) - 1:
            cur, nxt = children[i], children[i + 1]
            block_like = ["div", "section", "article", "ul", "ol", "table", "p", "h1", "h2", "h3"]
            if (cur.name == nxt.name
                and not cur.find_all(block_like)
                and not nxt.find_all(block_like)
                and cur.get_text(strip=True) == nxt.get_text(strip=True)
                and 0 < len(cur.get_text(strip=True)) <= max_len):
                nxt.decompose()
                children.pop(i + 1)
                removed += 1
            else:
                i += 1
    return removed


def dedup_repeated_media_links(root: Tag) -> int:
    """حذف تکرار لینک‌های <a> که فقط یک <img> دارند و src تکراری است."""
    seen: set[str] = set()
    removed = 0
    for a in root.find_all("a"):
        imgs = a.find_all("img")
        if len(imgs) == 1 and not a.get_text(strip=True):
            src = imgs[0].get("src") or imgs[0].get("data-src") or ""
            if src and src in seen:
                a.decompose()
                removed += 1
            elif src:
                seen.add(src)
    return removed


def dedup_duplicate_blocks(root: Tag, min_len: int = 40) -> int:
    """حذف بلوک‌های متنی تکراری (متن دقیقاً یکسان) در سطح DOM (فوتر موبایل/دسکتاپ)."""
    candidates = ("div", "section", "article", "aside", "footer",
                  "ul", "ol", "table", "p")
    seen: set[int] = set()
    removed = 0
    for el in list(root.find_all(candidates)):
        if el.parent is None:
            continue
        text = el.get_text(strip=True)
        if len(text) < min_len:
            continue
        has_big_child = any(
            len(ch.get_text(strip=True)) >= min_len
            for ch in el.find_all(candidates, recursive=False)
        )
        if has_big_child:
            continue
        h = hash(text)
        if h in seen:
            el.decompose()
            removed += 1
        else:
            seen.add(h)
    return removed


# --------------------------------------------------------------------------- #
# 3. تبدیل به Markdown (markdownify, MIT — replaces GPL html2text)
# --------------------------------------------------------------------------- #
def make_markdown(html_fragment: str, *, ignore_images: bool = False) -> str:
    """Convert an HTML fragment to Markdown using markdownify (MIT).

    Configuration mirrors the retrieval-friendly settings of the original
    link2rag: ATX headings, ``-`` bullets, no auto-linking of bare URLs,
    escaped asterisks (so ``*`` in text is not read as emphasis), and inline
    links kept as-is.
    """
    return _md_convert(
        html_fragment,
        heading_style="ATX",
        bullets="-",
        autolinks=False,
        escape_asterisks=True,
        escape_underscores=False,
        strip=["img"] if ignore_images else [],
    )


# --------------------------------------------------------------------------- #
# 4. پاک‌سازی قواعدی متن Markdown
# --------------------------------------------------------------------------- #
def decode_next_image(m: re.Match) -> str:
    """تبدیل /_next/image?url=ENCODED&w=...&q=... به URL واقعی تصویر (Next.js)."""
    raw = urllib.parse.unquote(m.group(1))
    return raw.split("&w=")[0].split("&q=")[0]


_SHARE_URL_RE = re.compile(
    r"(?:facebook\.com/sharer|api\.whatsapp\.com/send|whatsapp://send|"
    r"telegram\.me/share|t\.me/share/url|twitter\.com/share|x\.com/share|"
    r"linkedin\.com/sharing|pinterest\.com/pin/create|reddit\.com/submit|"
    r"mailto:\?subject=|mailto:\?body=)",
    re.IGNORECASE,
)


def postprocess_markdown(md: str) -> str:
    """نرمال‌سازی‌های امن و عام برای RAG."""
    # 1. دی‌کد /_next/image?url=...
    md = re.sub(r"/_next/image\?url=([^)\s\"']+)", decode_next_image, md)
    # 2. حذف لینک‌های اشتراک اجتماعی
    md = re.sub(
        r"\[[^\]]*\]\([^)]*(?:" + _SHARE_URL_RE.pattern + r")[^)]*\)",
        "", md, flags=re.IGNORECASE,
    )
    # 3. دی‌کد percent-encoding داخل URL لینک‌های markdown.
    def _dec_link(m: re.Match) -> str:
        return "](" + urllib.parse.unquote(m.group(1)) + ")"
    md = re.sub(r"\]\(([^)]*%[0-9A-Fa-f]{2}[^)]*)\)", _dec_link, md)
    # 4. حذف تصاویر data: URI (SVG base64 inline — نویز بزرگ)
    md = re.sub(r"!\[[^\]]*\]\(data:[^)]*\)", "", md)
    # 5. اصلاح نشانگرهای لیست فراری‌شده (باقیماننده از converterهای دیگر)
    md = re.sub(r"(?m)^(\d+)\\\.", r"\1.", md)
    # 6. حذف خط‌های تزئینی و placeholderهای خالی (___+)
    md = re.sub(r"_{3,}", "", md)
    # 7. تبدیل hard line break (دو فاصله در انتهای خط) به newline واقعی
    md = re.sub(r"  +\n", "\n", md)
    # 8. حذف خطوط جداکننده مصنوعی "* * *"
    md = re.sub(r"(?m)^\*\s*\*\s*\*\s*$", "", md)
    # 8b. حذف نشانگرهای emphasis از خطوط heading
    md = re.sub(
        r"(?m)^(#{1,6}\s+)(.*?)$",
        lambda m: m.group(1) + re.sub(r"\*{1,2}|_{1,2}", "", m.group(2)),
        md,
    )
    # 9. حذف هدینگ‌های خالی
    md = re.sub(r"(?m)^#{1,6}\s*$", "", md)
    # 10. حذف لینک‌های با متن خالی
    md = re.sub(r"\[\s*\]\([^)]*\)", "", md)
    # 11. حذف برچسب‌های مصنوعی slider
    md = re.sub(r"(?im)^\s*slide\s*$", "", md)
    # 12. نرمال‌سازی تورفتگی لیست‌ها
    md = re.sub(r"(?m)^  \* ", "- ", md)
    md = re.sub(r"(?m)^    \* ", "  - ", md)
    # 13. فشرده‌سازی خطوط خالی پشت‌سرهم
    md = re.sub(r"\n{3,}", "\n\n", md)
    # 14. حذف فاصله‌های پایان‌خطی اضافی
    md = re.sub(r"[ \t]+\n", "\n", md)
    return md.strip()


# --------------------------------------------------------------------------- #
# 4b. فیلتر محتوا: حذف وبلاگ‌ها و/یا محصولات ووکامرس
# --------------------------------------------------------------------------- #
_BLOG_PATH_PATTERNS = [
    r"/(?:blog|blogs|mag|magazine|news|posts|article|articles)(?:[-/]|$)",
    r"/(?:مجله|اخبار|وبلاگ|مقالات|مقاله)(?:[-/]|$)",
]
_PRODUCT_PATH_PATTERNS = [
    r"/products?(?:-category|-tag)?/",
    r"/(?:shop|store|cart|checkout|my-account|wishlist)(?:/|$)",
    r"/(?:فروشگاه|سبد-خرید|پرداخت|حساب-کاربری|محصول)(?:/|$)",
]
_PRODUCT_DOM_SELECTORS = [
    "ul.products", "div.products", ".product_list_widget",
    ".elementor-widget-woocommerce-products",
    ".elementor-widget-woocommerce-product-categories",
    ".elementor-widget-wc-categories",
    ".woocommerce-product-gallery",
    ".related.products", ".upsells.products", ".cross-sells",
]
_BLOG_DOM_SELECTORS = [
    ".elementor-widget-posts", ".elementor-widget-archive-posts",
    ".wp-block-latest-posts", ".post-list", ".blog-posts", ".article-list",
]


def compile_exclude_patterns(exclude_regexes, blogs: bool, products: bool):
    parts = []
    if blogs:
        parts += _BLOG_PATH_PATTERNS
    if products:
        parts += _PRODUCT_PATH_PATTERNS
    parts += list(exclude_regexes or [])
    return [re.compile(p, re.IGNORECASE) for p in parts]


def strip_excluded_widgets(soup, exclude_products: bool, exclude_blogs: bool) -> int:
    n = 0
    sels = []
    if exclude_products:
        sels += _PRODUCT_DOM_SELECTORS
    if exclude_blogs:
        sels += _BLOG_DOM_SELECTORS
    for sel in sels:
        for el in soup.select(sel):
            el.decompose()
            n += 1
    return n


def filter_excluded_links(md: str, patterns) -> str:
    if not patterns:
        return md

    def _excluded(url: str) -> bool:
        u = url.strip().lstrip("<").rstrip(">")
        try:
            path = urllib.parse.urlparse(u).path
        except Exception:
            path = u
        if not path:
            path = u
        return any(p.search(path) for p in patterns)

    link_line_re = re.compile(
        r"^\s*(?:[-*+]\s+|\d+\.\s+)?"
        r"\[(?:(?:[^\][]|\[[^\]]*\]\([^)]*\))*)\]\(([^)]+)\)\s*$"
    )
    img_link_re = re.compile(r"\[\s*!\[[^\]]*\]\([^)]*\)\s*\]\(([^)]+)\)")
    out = []
    for line in md.split("\n"):
        m = link_line_re.match(line)
        if m and _excluded(m.group(1)):
            continue
        line = img_link_re.sub(
            lambda mm: "" if _excluded(mm.group(1)) else mm.group(0), line,
        )
        line = re.sub(
            r"\[([^\]]*)\]\(([^)]+)\)",
            lambda mm: mm.group(1) if _excluded(mm.group(2)) else mm.group(0),
            line,
        )
        out.append(line)
    res = "\n".join(out)
    res = re.sub(r"(?m)^[ \t]+$", "", res)
    res = re.sub(r"[ \t]{3,}", " ", res)
    res = re.sub(r"\n{3,}", "\n\n", res)
    return res


# --------------------------------------------------------------------------- #
# 5. رندر مرورگر headless (اختیاری — فقط با agent-browser)
# --------------------------------------------------------------------------- #
class BrowserBackend:
    """پوشش ساده روی CLI از agent-browser (ابزار خارجی، نه وابستگی Python)."""

    def __init__(self, timeout_ms: int = 15000):
        self.timeout_ms = timeout_ms

    def _run(self, *args: str) -> str:
        cmd = ["agent-browser", *args]
        try:
            r = subprocess.run(cmd, capture_output=True, text=True,
                               timeout=self.timeout_ms / 1000 + 5)
        except FileNotFoundError:
            raise RuntimeError("agent-browser نصب نیست")
        if r.returncode != 0:
            raise RuntimeError(f"agent-browser خطا: {r.stderr.strip()[:200]}")
        return r.stdout

    def open(self, url: str) -> None:
        self._run("open", url)
        self._run("wait", "--load", "networkidle")

    def eval(self, expr: str):
        out = self._run("eval", expr).strip()
        try:
            return json.loads(out)
        except json.JSONDecodeError:
            m = re.search(r"(\{.*\}|\[.*\])\s*$", out, re.S)
            if m:
                return json.loads(m.group(1))
            return out

    def wait(self, ms: int) -> None:
        self._run("wait", str(ms))

    def close(self) -> None:
        try:
            self._run("close")
        except RuntimeError:
            pass


def render_page(url: str, timeout_ms: int = 15000, scroll_steps: int = 25,
                wait_ms: int = 1000) -> str:
    """رندر کامل صفحه در مرورگر headless با اسکرول تدریجی و snapshot-merge."""
    if not shutil.which("agent-browser"):
        raise RuntimeError("agent-browser نصب نیست؛ رندر پویا در دسترس نیست")
    b = BrowserBackend(timeout_ms=timeout_ms)
    b.open(url)
    b.wait(2500)
    snapshot_js = r"""
(() => {
  if (!window.__l2r_ids) {
    window.__l2r_ids = true;
    let i = 0;
    document.querySelectorAll('span,p,h1,h2,h3,h4,h5,h6,li,td,th,a,button,strong,em,b').forEach(el => {
      if (el.children.length === 0 && !el.getAttribute('data-l2r-id')) {
        el.setAttribute('data-l2r-id', 'l' + (i++));
      }
    });
  }
  const out = {};
  const vh = window.innerHeight;
  document.querySelectorAll('[data-l2r-id]').forEach(el => {
    const r = el.getBoundingClientRect();
    if (r.bottom > 0 && r.top < vh && r.height > 0 && r.width > 0) {
      const txt = (el.textContent || '').trim();
      if (txt && txt.length < 500) out[el.getAttribute('data-l2r-id')] = txt;
    }
  });
  return out;
})()
"""
    merged: dict[str, str] = {}
    for i in range(scroll_steps):
        frac = (i + 1) / scroll_steps
        b.eval(f"window.scrollTo(0, document.body.scrollHeight * {frac})")
        b.wait(wait_ms)
        snap = b.eval(snapshot_js)
        if isinstance(snap, dict):
            for k, v in snap.items():
                if k not in merged or len(v) > len(merged[k]):
                    merged[k] = v
    b.eval("window.scrollTo(0, document.body.scrollHeight)")
    b.wait(1500)
    snap = b.eval(snapshot_js)
    if isinstance(snap, dict):
        for k, v in snap.items():
            if k not in merged or len(v) > len(merged[k]):
                merged[k] = v
    merged_json = json.dumps(merged, ensure_ascii=False)
    inject_js = (
        "((M) => {"
        "Object.entries(M).forEach(([id, txt]) => {"
        "const el = document.querySelector('[data-l2r-id=\"' + id + '\"]');"
        "if (el && (el.textContent || '').trim() !== txt) el.textContent = txt;"
        "});"
        "return document.documentElement.outerHTML;"
        f"}})({merged_json})"
    )
    outer = b.eval(inject_js)
    b.close()
    return outer if isinstance(outer, str) else (str(outer) if outer else "")


_FIND_CLICK_JS_TMPL = r"""
((SEEN) => {
  const sel = 'button[aria-expanded="false"], [data-state="closed"][role="button"], [data-slot="trigger"][aria-expanded="false"]';
  const triggers = Array.from(document.querySelectorAll(sel))
    .filter(b => b.getClientRects().length)
    .filter(b => {
      const t = (b.innerText || b.textContent || '').trim();
      if (t.length < 3) return false;
      if (b.closest('nav')) return false;
      const hp = b.getAttribute('aria-haspopup');
      if (hp === 'menu' || hp === 'true') return false;
      return true;
    });
  const unseen = triggers.filter(b => {
    const t = (b.innerText || b.textContent || '').trim().slice(0, 200);
    return !SEEN.includes(t);
  });
  if (!unseen.length) return {done: true, remaining: 0};
  const btn = unseen[0];
  const triggerText = (btn.innerText || btn.textContent || '').trim().slice(0, 200);
  btn.scrollIntoView({block: 'center'});
  btn.click();
  return {done: false, remaining: unseen.length, triggerText};
})(__SEEN__)
"""
_READ_OPEN_PANEL_JS = r"""
(() => {
  const sel = 'button[aria-expanded="true"], [data-state="open"][role="button"], [data-slot="trigger"][aria-expanded="true"]';
  const open = Array.from(document.querySelectorAll(sel)).filter(b => b.getClientRects().length);
  if (!open.length) return null;
  const btn = open[0];
  const triggerText = (btn.innerText || btn.textContent || '').trim().slice(0, 200);
  const ctrl = btn.getAttribute('aria-controls');
  let panel = ctrl ? document.getElementById(ctrl) : null;
  if (!panel) {
    const item = btn.closest('[data-state]') ? btn.closest('[data-state]').parentElement : btn.parentElement;
    panel = item && item.querySelector('[data-state="open"], [role="region"], [data-slot="content"]');
  }
  if (!panel) panel = btn.parentElement && btn.parentElement.nextElementSibling;
  let panelText = panel ? panel.textContent.replace(/\s+/g, ' ').trim() : '';
  if (panelText.startsWith(triggerText)) panelText = panelText.slice(triggerText.length).trim();
  return {triggerText, panelText: panelText.slice(0, 4000)};
})()
"""
_CLOSE_OPEN_JS = r"""
(() => {
  const sel = 'button[aria-expanded="true"], [data-state="open"][role="button"], [data-slot="trigger"][aria-expanded="true"]';
  const open = Array.from(document.querySelectorAll(sel)).filter(b => b.getClientRects().length);
  if (open.length) open[0].click();
  return open.length;
})()
"""


def expand_accordions(url: str, max_items: int = 40, wait_ms: int = 700,
                      timeout_ms: int = 15000, static_md: str = "") -> list[dict]:
    """باز کردن آکاردئون‌های بسته و استخراج (متن trigger, متن panel)."""
    if not shutil.which("agent-browser"):
        print("هشدار: agent-browser نصب نیست؛ بخش dynamic نادیده گرفته شد.",
              file=sys.stderr)
        return []
    static_norm = re.sub(r"\s+", "", static_md)

    def already_in_static(panel_text: str) -> bool:
        if not panel_text or not static_norm:
            return False
        return re.sub(r"\s+", "", panel_text)[:80] in static_norm

    items: list[dict] = []
    try:
        b = BrowserBackend(timeout_ms=timeout_ms)
        b.open(url)
        seen: list[str] = []
        for _ in range(max_items):
            js = _FIND_CLICK_JS_TMPL.replace("__SEEN__", json.dumps(seen, ensure_ascii=False))
            res = b.eval(js)
            if not isinstance(res, dict) or res.get("done"):
                break
            trigger_text = (res.get("triggerText") or "").strip()
            if not trigger_text:
                b.wait(wait_ms)
                continue
            seen.append(trigger_text)
            b.wait(wait_ms)
            panel = b.eval(_READ_OPEN_PANEL_JS)
            b.wait(max(200, wait_ms // 3))
            b.eval(_CLOSE_OPEN_JS)
            b.wait(max(200, wait_ms // 3))
            if isinstance(panel, dict) and panel.get("panelText"):
                ptext = panel["panelText"]
                if already_in_static(ptext):
                    continue
                items.append({"q": panel.get("triggerText") or trigger_text, "a": ptext})
        b.close()
    except RuntimeError as e:
        print(f"هشدار: خطای مرورگر ({e}); بخش dynamic ناقص ماند.", file=sys.stderr)
    return items


def render_dynamic_section(items: list[dict]) -> str:
    if not items:
        return ""
    lines = ["", "## محتوای تعاملی (Accordion-expanded content)", ""]
    seen: set[str] = set()
    for it in items:
        q = (it.get("q") or "").strip()
        a = (it.get("a") or "").strip()
        if q and a.startswith(q):
            a = a[len(q):].strip()
        if not a or q in seen:
            continue
        seen.add(q)
        lines.append(f"### {q}")
        lines.append(a)
        lines.append("")
    return "\n".join(lines) if len(lines) > 3 else ""


# --------------------------------------------------------------------------- #
# 6. فراداده + مونتاژ نهایی
# --------------------------------------------------------------------------- #
def make_slug(url: str) -> str:
    p = urllib.parse.urlparse(url)
    base = (p.netloc + p.path).replace("/", "__").strip("_")
    slug = re.sub(r"[^A-Za-z0-9\u0600-\u06FF._-]+", "-", base)
    slug = re.sub(r"-+", "-", slug).strip("-")
    return slug or "page"


def extract_meta(raw_html: str, url: str, title_override: str | None = None,
                 lang_override: str | None = None) -> dict:
    """استخراج فراداده از HTML خام (قبل از strip)."""
    soup = BeautifulSoup(raw_html, "html.parser")
    title = title_override or ""
    if not title:
        t = soup.find("title")
        if t and t.get_text(strip=True):
            title = t.get_text(strip=True)
        else:
            og = soup.find("meta", attrs={"property": "og:title"})
            if og and og.get("content"):
                title = og["content"]
            else:
                h1 = soup.find("h1")
                title = h1.get_text(strip=True) if h1 and h1.get_text(strip=True) else url
    lang = lang_override or ""
    if not lang:
        html_tag = soup.find("html")
        lang = html_tag.get("lang", "") if html_tag else ""
    desc = ""
    md = soup.find("meta", attrs={"name": "description"})
    if not md or not md.get("content"):
        md = soup.find("meta", attrs={"property": "og:description"})
    if md and md.get("content"):
        desc = md["content"]
    return {"title": title, "lang": lang, "description": desc}


def build_frontmatter(meta: dict, url: str, captured: str, method: str) -> str:
    fm = ["---", f"source: {url}", f"title: {meta['title']}"]
    if meta["lang"]:
        fm.append(f"lang: {meta['lang']}")
    if meta["description"]:
        d = meta["description"].replace('"', '\\"')
        fm.append(f'description: "{d}"')
    fm.append(f"captured: {captured}")
    fm.append(f"method: {method}")
    fm.append("---")
    return "\n".join(fm)


# --------------------------------------------------------------------------- #
# 7. خط اصلی: HTML → Markdown (تابع مرکزی که UrlParser از آن استفاده می‌کند)
# --------------------------------------------------------------------------- #
def html_to_markdown(
    html: str,
    url: str,
    *,
    selector: str | None = None,
    strip_forms: bool = False,
    ignore_images: bool = False,
    exclude_regexes: Iterable[str] | None = None,
    exclude_blogs: bool = False,
    exclude_products: bool = False,
    dedup_siblings: bool = True,
    dedup_blocks: bool = True,
    title: str | None = None,
    lang: str | None = None,
) -> tuple[str, dict]:
    """Convert a raw HTML document to retrieval-ready Markdown + metadata.

    Returns ``(markdown, meta)`` where ``markdown`` includes a YAML front-matter
    block and a source header. The optional dynamic rendering (accordions /
    scroll hydration) is handled separately by :func:`render_dynamic_markdown`
    so the static path stays dependency-free.
    """
    captured = dt.date.today().isoformat()
    soup = parse_html(html, selector, strip_forms)
    body = soup.body or soup
    if exclude_products or exclude_blogs:
        strip_excluded_widgets(soup, exclude_products, exclude_blogs)
    if dedup_siblings:
        dedup_duplicate_siblings(body)
    dedup_repeated_media_links(body)
    if dedup_blocks:
        dedup_duplicate_blocks(body)

    md = make_markdown(str(body), ignore_images=ignore_images)
    md = postprocess_markdown(md)

    patterns = compile_exclude_patterns(exclude_regexes, exclude_blogs, exclude_products)
    if patterns:
        md = filter_excluded_links(md, patterns)

    meta = extract_meta(html, url, title, lang)
    front = build_frontmatter(meta, url, captured, "static HTML")
    header_note = (
        f"\n# {meta['title']}\n\n"
        f"> منبع: {url}\n"
        f"> استخراج‌شده با docrag (html_to_md) برای بارگذاری در RAG.\n\n"
    )
    final = f"{front}\n{header_note}{md}\n"
    return final, meta


def render_dynamic_markdown(
    url: str,
    static_md: str,
    *,
    render: bool = False,
    dynamic: bool = False,
    scroll_steps: int = 25,
    scroll_wait_ms: int = 1000,
    max_accordions: int = 40,
    wait_ms: int = 700,
    browser_timeout: int = 15000,
) -> tuple[str, str]:
    """Optionally enrich ``static_md`` with headless-rendered content.

    Returns ``(markdown, method)``. When neither ``render`` nor ``dynamic`` is
    set, or ``agent-browser`` is absent, returns the input unchanged with
    method ``"static HTML"``.
    """
    method = "static HTML"
    md = static_md
    if render:
        try:
            rendered_html = render_page(url, browser_timeout, scroll_steps, scroll_wait_ms)
            if rendered_html:
                md2, _ = html_to_markdown(rendered_html, url)
                if len(md2) >= len(md) * 0.5:
                    md = md2
                    method = "rendered HTML (headless + scroll)"
        except RuntimeError as e:
            print(f"هشدار: {e}", file=sys.stderr)
    if dynamic:
        items = expand_accordions(url, max_accordions, wait_ms, browser_timeout, static_md=md)
        dyn = render_dynamic_section(items)
        if dyn:
            md = md.rstrip() + "\n\n" + dyn
            method = ("rendered HTML + accordions" if render
                      else "static HTML + headless-rendered accordions")
    return md, method
