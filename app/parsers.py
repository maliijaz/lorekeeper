"""E-book parsing: EPUB, PDF, MOBI/AZW/AZW3, FB2, DOCX, HTML, TXT/MD, RTF -> chapters -> fixed pages.

Every format is reduced to a stream of blocks (heading / paragraph / break), chapterized, then split
into pages of roughly `page_chars` characters. Pages are the unit used for spoiler boundaries, so the
page numbering is independent of the reader's font size or screen.
"""
import re
import shutil
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

from bs4 import BeautifulSoup, XMLParsedAsHTMLWarning
import warnings

warnings.filterwarnings("ignore", category=XMLParsedAsHTMLWarning)

SUPPORTED = {".epub", ".pdf", ".mobi", ".azw", ".azw3", ".fb2", ".docx", ".html", ".htm", ".xhtml",
             ".txt", ".md", ".rtf"}

BLOCK_TAGS = {"p", "h1", "h2", "h3", "h4", "h5", "h6", "li", "blockquote", "pre", "div", "section",
              "article", "td", "th", "dd", "dt", "figcaption", "caption", "tr", "table", "ul", "ol",
              "body", "header", "footer", "aside", "main", "nav", "center"}
HEADING_TAGS = {"h1", "h2", "h3"}
HEADING_RE = re.compile(
    r"^\s*(chapter|book|part|prologue|epilogue|interlude|act|volume)\b[\s\w.:,'’\-–—]{0,80}$", re.I)
ROMAN_RE = re.compile(r"^\s*[IVXLC]+\.?\s*$")


@dataclass
class Block:
    kind: str  # "h" heading, "p" paragraph, "break" hard chapter boundary
    text: str = ""


@dataclass
class Chapter:
    title: str
    paragraphs: list = field(default_factory=list)


@dataclass
class ParsedBook:
    title: str
    author: str
    chapters: list


def clean(text: str) -> str:
    text = text.replace("­", "").replace("\xa0", " ").replace("​", "")
    return re.sub(r"\s+", " ", text).strip()


# ---------------------------------------------------------------- HTML-ish sources

def html_blocks(markup) -> list:
    soup = BeautifulSoup(markup, "lxml")
    for bad in soup(["script", "style", "head", "svg", "math"]):
        bad.decompose()
    body = soup.body or soup
    out = []

    def walk(el):
        # Emit leaf-level block elements; inline content directly inside a container becomes its own block.
        buf = []

        def flush():
            t = clean("".join(buf))
            if t:
                out.append(Block("p", t))
            buf.clear()

        for child in el.children:
            name = getattr(child, "name", None)
            if name is None:
                buf.append(str(child))
            elif name == "br":
                buf.append(" ")
            elif name in BLOCK_TAGS:
                flush()
                if name in HEADING_TAGS:
                    t = clean(child.get_text(" "))
                    if t:
                        out.append(Block("h", t))
                elif any(getattr(c, "name", None) in BLOCK_TAGS for c in child.descendants):
                    walk(child)
                else:
                    t = clean(child.get_text(" "))
                    if t:
                        out.append(Block("p", t))
            else:
                buf.append(child.get_text(" "))
        flush()

    walk(body)
    return out


def parse_epub(path: Path):
    import ebooklib
    from ebooklib import epub

    book = epub.read_epub(str(path), {"ignore_ncx": False})
    title = _first_meta(book, "title") or path.stem
    author = _first_meta(book, "creator") or ""
    blocks = []
    for idref, *_ in book.spine:
        item = book.get_item_with_id(idref)
        if item is None or item.get_type() != ebooklib.ITEM_DOCUMENT:
            continue
        if isinstance(item, epub.EpubNav):
            continue
        doc_blocks = html_blocks(item.get_content())
        if doc_blocks:
            blocks.append(Block("break"))
            blocks.extend(doc_blocks)
    return title, author, blocks


def _first_meta(book, name):
    try:
        vals = book.get_metadata("DC", name)
        return clean(vals[0][0]) if vals else ""
    except Exception:
        return ""


def parse_html_file(path: Path):
    raw = path.read_bytes()
    soup = BeautifulSoup(raw, "lxml")
    title = clean(soup.title.get_text()) if soup.title else path.stem
    return title, "", html_blocks(raw)


def parse_mobi(path: Path):
    import mobi

    tmpdir, filepath = mobi.extract(str(path))
    try:
        fp = Path(filepath)
        if fp.suffix.lower() == ".epub":
            title, author, blocks = parse_epub(fp)
        elif fp.suffix.lower() == ".pdf":
            title, author, blocks = parse_pdf(fp)
        else:
            title, author, blocks = parse_html_file(fp)
        if title in (fp.stem, "", "Unknown"):
            title = path.stem
        return title, author, blocks
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


# ---------------------------------------------------------------- PDF

def parse_pdf(path: Path):
    import fitz

    doc = fitz.open(str(path))
    meta = doc.metadata or {}
    toc_pages = {}
    for level, t, pg in doc.get_toc(simple=True):
        if level <= 2 and pg >= 1:
            toc_pages.setdefault(pg - 1, clean(t))
    blocks = []
    pending = ""
    for pno, page in enumerate(doc):
        if pno in toc_pages:
            if pending:
                blocks.append(Block("p", clean(pending)))
                pending = ""
            blocks.append(Block("break"))
            blocks.append(Block("h", toc_pages[pno]))
        height = page.rect.height
        for x0, y0, x1, y1, text, *_ in page.get_text("blocks", sort=True):
            text = text.strip()
            if not text:
                continue
            # Drop running headers/footers and bare page numbers.
            if (y1 < height * 0.07 or y0 > height * 0.93) and len(text) < 80:
                continue
            if re.fullmatch(r"[\divxlcIVXLC\-–— ]{1,8}", text):
                continue
            text = re.sub(r"(\w)-\n(\w)", r"\1\2", text)
            if not toc_pages and len(text) < 90 and HEADING_RE.match(text.split("\n")[0]):
                if pending:
                    blocks.append(Block("p", clean(pending)))
                    pending = ""
                blocks.append(Block("break"))
                blocks.append(Block("h", clean(text)))
                continue
            text = text.replace("\n", " ")
            # A block that doesn't end a sentence and is followed by a lowercase start continues the paragraph.
            if pending and (pending[-1] not in ".!?\"'”’:" or text[:1].islower()):
                pending += " " + text
            else:
                if pending:
                    blocks.append(Block("p", clean(pending)))
                pending = text
    if pending:
        blocks.append(Block("p", clean(pending)))
    return clean(meta.get("title") or "") or path.stem, clean(meta.get("author") or ""), blocks


# ---------------------------------------------------------------- plain text family

def read_text(path: Path) -> str:
    raw = path.read_bytes()
    for enc in ("utf-8-sig", "utf-16", "cp1252", "latin-1"):
        try:
            text = raw.decode(enc)
            if enc == "utf-16" and not raw[:2] in (b"\xff\xfe", b"\xfe\xff"):
                continue
            return text
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def text_blocks(text: str) -> list:
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    paras = re.split(r"\n\s*\n", text)
    if len(paras) < 5:  # one paragraph per line
        paras = text.split("\n")
    out = []
    for p in paras:
        lines = [l.strip() for l in p.split("\n") if l.strip()]
        if not lines:
            continue
        if len(lines) <= 2 and (HEADING_RE.match(lines[0]) or ROMAN_RE.match(lines[0])):
            out.append(Block("break"))
            out.append(Block("h", clean(" ".join(lines))))
            continue
        if lines[0].startswith("#"):
            out.append(Block("break"))
            out.append(Block("h", clean(lines[0].lstrip("#"))))
            lines = lines[1:]
            if not lines:
                continue
        joined = re.sub(r"(\w)-\s+(\w)", r"\1\2", " ".join(lines)) if len(lines) > 1 else lines[0]
        out.append(Block("p", clean(joined)))
    return out


def parse_txt(path: Path):
    return path.stem.replace("_", " "), "", text_blocks(read_text(path))


def parse_rtf(path: Path):
    from striprtf.striprtf import rtf_to_text

    return path.stem.replace("_", " "), "", text_blocks(rtf_to_text(read_text(path)))


def parse_docx(path: Path):
    import docx

    d = docx.Document(str(path))
    blocks = []
    for p in d.paragraphs:
        t = clean(p.text)
        if not t:
            continue
        style = (p.style.name or "").lower() if p.style is not None else ""
        if style.startswith("heading") or style == "title" or HEADING_RE.match(t):
            blocks.append(Block("break"))
            blocks.append(Block("h", t))
        else:
            blocks.append(Block("p", t))
    cp = d.core_properties
    return clean(cp.title or "") or path.stem, clean(cp.author or ""), blocks


def parse_fb2(path: Path):
    from lxml import etree

    raw = path.read_bytes()
    if zipfile.is_zipfile(path):  # .fb2.zip
        with zipfile.ZipFile(path) as z:
            raw = z.read(next(n for n in z.namelist() if n.endswith(".fb2")))
    root = etree.fromstring(raw, parser=etree.XMLParser(recover=True, huge_tree=True))

    def local(el):
        return etree.QName(el).localname if isinstance(el.tag, str) else ""

    title, author = path.stem, ""
    for el in root.iter():
        n = local(el)
        if n == "book-title" and el.text:
            title = clean(el.text)
        elif n == "author" and not author:
            author = clean(" ".join(t for t in el.itertext()))
    blocks = []
    for body in (e for e in root if local(e) == "body"):
        if body.get("name") in ("notes", "comments"):
            continue
        for el in body.iter():
            n = local(el)
            if n == "section":
                blocks.append(Block("break"))
            elif n == "title":
                t = clean(" ".join(el.itertext()))
                if t:
                    blocks.append(Block("h", t))
            elif n in ("p", "v", "subtitle", "text-author") and local(el.getparent()) != "title":
                t = clean(" ".join(el.itertext()))
                if t:
                    blocks.append(Block("p", t))
    return title, author, blocks


PARSERS = {
    ".epub": parse_epub, ".pdf": parse_pdf, ".mobi": parse_mobi, ".azw": parse_mobi, ".azw3": parse_mobi,
    ".fb2": parse_fb2, ".docx": parse_docx, ".html": parse_html_file, ".htm": parse_html_file,
    ".xhtml": parse_html_file, ".txt": parse_txt, ".md": parse_txt, ".rtf": parse_rtf,
}


# ---------------------------------------------------------------- assembling

def strip_boilerplate(blocks: list) -> list:
    """Remove Project Gutenberg style licence header/footer if present."""
    start, end = 0, len(blocks)
    for i, b in enumerate(blocks):
        if b.kind != "break" and re.search(r"\*\*\*\s*START OF (THE|THIS) PROJECT GUTENBERG", b.text, re.I):
            start = i + 1
            break
    for i in range(start, len(blocks)):
        b = blocks[i]
        if b.kind != "break" and re.search(r"\*\*\*\s*END OF (THE|THIS) PROJECT GUTENBERG", b.text, re.I):
            end = i
            break
    return blocks[start:end]


TOC_TITLE_RE = re.compile(r"^\s*(table of contents|contents|list of illustrations|illustrations|index)\s*$", re.I)


FRONT_TITLE_RE = re.compile(r"^\s*(copyright|title page|also by .*|praise for .*|about the (author|publisher)|"
                            r"acknowledge?ments|newsletter|more from .*)\s*$", re.I)
PUBLISHING_RE = re.compile(r"copyright\s*(?:©|\(c\)|\d{4})|all rights reserved|\bISBN\b|library of congress|"
                           r"printed in|published (?:in the united states )?by|ebook edition|work of fiction", re.I)


def is_front_matter(ch: Chapter) -> bool:
    """Copyright / publisher pages: no story content, but they produce junk codex entries."""
    body = [p.removeprefix("# ") for p in ch.paragraphs]
    if FRONT_TITLE_RE.match(ch.title) or (body and FRONT_TITLE_RE.match(body[0])):
        return True
    text = " ".join(body)
    hits = {m.group(0).lower()[:12] for m in PUBLISHING_RE.finditer(text)}
    return len(text) < 5000 and len(hits) >= 2


def is_toc(ch: Chapter) -> bool:
    """Printed tables of contents list later chapter titles -> spoilers. The reader has its own chapter menu."""
    if TOC_TITLE_RE.match(ch.title):
        return True
    first = next((p.removeprefix("# ") for p in ch.paragraphs if p.strip()), "")
    if TOC_TITLE_RE.match(first):
        return True
    body = [p for p in ch.paragraphs if not p.startswith("# ")]
    # Many short lines that look like chapter headings = a contents page.
    return len(body) >= 6 and sum(1 for p in body if HEADING_RE.match(p) and len(p) < 90) > 0.6 * len(body)


def chapterize(blocks: list) -> list:
    chapters = [Chapter("")]
    for b in blocks:
        cur = chapters[-1]
        if b.kind == "break":
            if cur.paragraphs:
                chapters.append(Chapter(""))
        elif b.kind == "h":
            has_body = any(not p.startswith("# ") for p in cur.paragraphs)
            if has_body:
                chapters.append(Chapter(b.text[:120]))
            elif not cur.title or HEADING_RE.match(b.text):
                cur.title = b.text[:120]
            chapters[-1].paragraphs.append("# " + b.text)
        else:
            chapters[-1].paragraphs.append(b.text)
    chapters = [c for c in chapters if any(not p.startswith("# ") for p in c.paragraphs)
                and not is_toc(c) and not is_front_matter(c)]
    for i, c in enumerate(chapters):
        if not c.title:
            first = next(p for p in c.paragraphs if not p.startswith("# "))
            c.title = f"Section {i + 1}" if len(first) > 60 else first
    return chapters


def parse_book(path: Path) -> ParsedBook:
    ext = path.suffix.lower()
    if ext not in PARSERS:
        raise ValueError(f"Unsupported format: {ext}")
    title, author, blocks = PARSERS[ext](path)
    blocks = strip_boilerplate(blocks)
    chapters = chapterize(blocks)
    if not chapters:
        raise ValueError("No readable text found in this file (scanned PDFs need OCR first).")
    return ParsedBook(title=title or path.stem, author=author, chapters=chapters)


def split_long(paragraph: str, limit: int) -> list:
    if len(paragraph) <= limit:
        return [paragraph]
    sentences = re.split(r"(?<=[.!?”\"])\s+", paragraph)
    out, cur = [], ""
    for s in sentences:
        if cur and len(cur) + len(s) > limit:
            out.append(cur)
            cur = s
        else:
            cur = f"{cur} {s}".strip()
    if cur:
        out.append(cur)
    return out


def paginate(chapters: list, page_chars: int):
    """Returns list of (chapter_idx, text) pages. Each chapter starts on a new page."""
    pages = []
    for ci, ch in enumerate(chapters):
        cur, size = [], 0
        for para in ch.paragraphs:
            for piece in split_long(para, page_chars):
                if cur and size + len(piece) > page_chars and size > page_chars * 0.4:
                    pages.append((ci, "\n\n".join(cur)))
                    cur, size = [], 0
                cur.append(piece)
                size += len(piece)
        if cur:
            pages.append((ci, "\n\n".join(cur)))
    return pages
