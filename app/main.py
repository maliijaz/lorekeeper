"""FastAPI app: library, reader, spoiler-aware codex, Q&A, settings."""
import logging
import re
import uuid
from pathlib import Path

from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import codex, db
from . import setup
from .config import BOOKS_DIR, DEFAULTS, STATIC_DIR, book_path, load_settings, save_settings
from .extraction import Worker, clean_front_matter, ingest, rebuild_from_raw
from .llm import LLM, LLMError
from .parsers import SUPPORTED

logging.basicConfig(level=logging.INFO)
app = FastAPI(title="Lorekeeper")
db.init_db()
worker = Worker()

# Tidy books imported with older parser versions (before the worker touches them).
for b in db.rows("SELECT id FROM books WHERE num_pages > 0"):
    try:
        clean_front_matter(b["id"])
    except Exception:  # noqa: BLE001 - never block startup on a cleanup
        logging.exception("front matter cleanup failed for %s", b["id"])

# Resume anything that was mid-flight when the server stopped.
for b in db.rows("SELECT id FROM books WHERE status IN ('queued','processing') ORDER BY added_at"):
    worker.enqueue(b["id"])


def get_book(book_id: str) -> dict:
    b = db.row("SELECT * FROM books WHERE id=?", (book_id,))
    if not b:
        raise HTTPException(404, "Book not found")
    return b


# ------------------------------------------------------------------ library

@app.get("/api/books")
def books():
    return db.rows("SELECT id, title, author, format, num_pages, status, stage, progress, processed_page, "
                   "error, last_page, max_page, added_at FROM books ORDER BY added_at DESC")


@app.post("/api/books")
async def upload(file: UploadFile = File(...), auto_process: bool = True):
    ext = Path(file.filename or "").suffix.lower()
    if ext not in SUPPORTED:
        raise HTTPException(400, f"Unsupported format {ext}. Supported: {', '.join(sorted(SUPPORTED))}")
    book_id = uuid.uuid4().hex[:12]
    safe = re.sub(r"[^\w.\- ]", "_", Path(file.filename).name)
    dest = BOOKS_DIR / f"{book_id}_{safe}"
    dest.write_bytes(await file.read())
    with db.tx() as c:
        c.execute("INSERT INTO books(id, title, format, file_path, status) VALUES (?,?,?,?,?)",
                  (book_id, Path(safe).stem, ext.lstrip("."), dest.name, "queued" if auto_process else "paused"))
    try:
        await run_in_threadpool(ingest, book_id, dest)
    except Exception as e:
        db.delete_book(book_id)
        dest.unlink(missing_ok=True)
        raise HTTPException(422, f"Could not read this book: {e}")
    if auto_process:
        worker.enqueue(book_id)
    return get_book(book_id)


@app.delete("/api/books/{book_id}")
def delete(book_id: str):
    b = get_book(book_id)
    db.delete_book(book_id)
    book_path(b["file_path"]).unlink(missing_ok=True)
    return {"ok": True}


@app.post("/api/books/{book_id}/process")
def process(book_id: str):
    get_book(book_id)
    with db.tx() as c:
        c.execute("UPDATE books SET status='queued', stage='', error=NULL WHERE id=?", (book_id,))
    worker.enqueue(book_id)
    return {"ok": True}


@app.post("/api/books/{book_id}/pause")
def pause(book_id: str):
    get_book(book_id)
    with db.tx() as c:
        c.execute("UPDATE books SET status='paused', stage='Paused' WHERE id=? AND status IN ('queued','processing')",
                  (book_id,))
    return {"ok": True}


@app.post("/api/books/{book_id}/reset")
def reset(book_id: str):
    """Throw away the codex and re-extract from scratch (e.g. after switching to a stronger model)."""
    b = get_book(book_id)
    if b["status"] == "processing":
        raise HTTPException(409, "Pause processing first")
    with db.tx() as c:
        ids = [r[0] for r in c.execute("SELECT id FROM entities WHERE book_id=?", (book_id,))]
        db.clear_entities(c, ids)
        c.execute("UPDATE chunks SET done=0, raw=NULL WHERE book_id=?", (book_id,))
        c.execute("DELETE FROM kv WHERE key=?", (f"dedupe:{book_id}",))
        c.execute("UPDATE books SET processed_page=0, progress=0, status='queued', stage='', error=NULL WHERE id=?", (book_id,))
    worker.enqueue(book_id)
    return {"ok": True}


@app.post("/api/books/{book_id}/rebuild")
async def rebuild(book_id: str):
    """Rebuild the codex from stored model outputs (after resolver upgrades), then finish/refine it."""
    b = get_book(book_id)
    if b["status"] == "processing":
        raise HTTPException(409, "Pause processing first")
    await run_in_threadpool(rebuild_from_raw, book_id)
    with db.tx() as c:
        c.execute("UPDATE books SET status='queued', stage='', error=NULL WHERE id=?", (book_id,))
    worker.enqueue(book_id)
    return {"ok": True}


# ------------------------------------------------------------------ reading

BARE_TITLE = re.compile(r"[\dIVXLCivxlc.\s]+|Section \d+")


def display_title(book_id: str, ch: dict) -> str:
    """Many e-books title chapters just "4" and put the real name in the next line ("THE GIFT")."""
    title = ch["title"] or ""
    if not BARE_TITLE.fullmatch(title):
        return title
    page = db.row("SELECT text FROM pages WHERE book_id=? AND page_no=?", (book_id, ch["start_page"]))
    if not page:
        return title
    for para in page["text"].split("\n\n")[:3]:
        para = para.removeprefix("# ").strip()
        if not para or para == title or BARE_TITLE.fullmatch(para):
            continue
        if len(para) > 60 or re.search(r"[.!?,;:\"”’…]$", para):
            break
        name = para.title() if para.isupper() else para
        return name if title.startswith("Section") else f"{title} · {name}"
    return title


@app.get("/api/books/{book_id}")
def book(book_id: str):
    b = get_book(book_id)
    b["chapters"] = db.rows("SELECT idx, title, start_page FROM chapters WHERE book_id=? ORDER BY start_page",
                            (book_id,))
    for ch in b["chapters"]:
        ch["title"] = display_title(book_id, ch)
    return b


@app.get("/api/books/{book_id}/pages")
def page_range(book_id: str, start: int, end: int):
    """Consecutive pages for the continuous reader (at most 40 per request)."""
    end = min(end, start + 39)
    return db.rows("SELECT page_no, chapter_idx, text FROM pages WHERE book_id=? AND page_no BETWEEN ? AND ? "
                   "ORDER BY page_no", (book_id, start, end))


@app.get("/api/books/{book_id}/pages/{page_no}")
def page(book_id: str, page_no: int):
    p = db.row("SELECT page_no, chapter_idx, text FROM pages WHERE book_id=? AND page_no=?", (book_id, page_no))
    if not p:
        raise HTTPException(404, "Page not found")
    return p


class Progress(BaseModel):
    page: int
    max_page: int | None = None  # explicit override ("I've read up to page N")


@app.post("/api/books/{book_id}/progress")
def progress(book_id: str, body: Progress):
    b = get_book(book_id)
    page_no = max(1, min(body.page, b["num_pages"] or 1))
    new_max = max(b["max_page"] or 1, page_no) if body.max_page is None else max(1, min(body.max_page, b["num_pages"]))
    with db.tx() as c:
        c.execute("UPDATE books SET last_page=?, max_page=? WHERE id=?", (page_no, new_max, book_id))
    return {"last_page": page_no, "max_page": new_max}


# ------------------------------------------------------------------ codex

@app.get("/api/books/{book_id}/codex")
def codex_list(book_id: str, upto: int | None = None, type: str = "", q: str = ""):
    b = get_book(book_id)
    return codex.list_entities(book_id, codex.clamp_upto(b, upto), type, q)


@app.get("/api/books/{book_id}/entities/{eid}")
def entity(book_id: str, eid: int, upto: int | None = None):
    b = get_book(book_id)
    d = codex.entity_detail(book_id, eid, codex.clamp_upto(b, upto))
    if not d:
        raise HTTPException(404, "Not found (or not yet revealed at your reading position)")
    return codex.public(d)


@app.post("/api/books/{book_id}/entities/{eid}/summary")
async def entity_summary(book_id: str, eid: int, upto: int | None = None):
    b = get_book(book_id)
    try:
        text = await run_in_threadpool(codex.summarize, book_id, eid, codex.clamp_upto(b, upto))
    except (LLMError, ValueError) as e:
        raise HTTPException(502, str(e))
    return {"summary": text}


@app.get("/api/books/{book_id}/highlights")
def highlights(book_id: str, upto: int | None = None):
    b = get_book(book_id)
    return codex.highlights(book_id, codex.clamp_upto(b, upto))


class Question(BaseModel):
    question: str
    upto: int | None = None


@app.post("/api/books/{book_id}/ask")
async def ask(book_id: str, body: Question):
    b = get_book(book_id)
    try:
        return await run_in_threadpool(codex.ask, book_id, body.question, codex.clamp_upto(b, body.upto))
    except LLMError as e:
        raise HTTPException(502, str(e))


# ------------------------------------------------------------------ settings

@app.get("/api/settings")
def get_settings():
    s = load_settings()
    s["openai_api_key"] = "********" if s.get("openai_api_key") else ""
    return s


@app.post("/api/settings")
def set_settings(body: dict):
    body = {k: v for k, v in body.items() if k in DEFAULTS}
    if body.get("openai_api_key") == "********":
        body.pop("openai_api_key")
    save_settings(body)
    return get_settings()


@app.get("/api/ping")
def ping():
    return {"app": "lorekeeper"}


@app.get("/api/setup")
def setup_status():
    return setup.status()


@app.post("/api/setup/pull")
def setup_pull():
    return setup.pull_model()


@app.post("/api/setup/start-ollama")
async def setup_start_ollama():
    return {"ok": await run_in_threadpool(setup.start_ollama)}


@app.get("/api/health")
def health():
    info = {"llm": LLM().health(), "provider": load_settings()["provider"]}
    try:
        import torch
        info["gpu"] = torch.cuda.get_device_name(0) if torch.cuda.is_available() else None
    except Exception:
        info["gpu"] = None
    return info


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.get("/")
def index():
    return FileResponse(STATIC_DIR / "index.html")

