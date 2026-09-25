"""SQLite storage. One connection per thread; WAL so the reader UI stays responsive during extraction."""
import sqlite3
import threading
from contextlib import contextmanager

from .config import DB_PATH

SCHEMA = """
CREATE TABLE IF NOT EXISTS books (
    id TEXT PRIMARY KEY,
    title TEXT, author TEXT, format TEXT, file_path TEXT,
    added_at TEXT DEFAULT (datetime('now')),
    num_pages INTEGER DEFAULT 0,
    status TEXT DEFAULT 'queued',          -- queued | processing | paused | done | error
    stage TEXT DEFAULT '',
    progress REAL DEFAULT 0,
    processed_page INTEGER DEFAULT 0,      -- extraction is complete up to this page
    error TEXT,
    last_page INTEGER DEFAULT 1,           -- page the reader is currently on
    max_page INTEGER DEFAULT 1             -- furthest page the reader has reached
);
CREATE TABLE IF NOT EXISTS chapters (
    book_id TEXT, idx INTEGER, title TEXT, start_page INTEGER,
    PRIMARY KEY (book_id, idx)
);
CREATE TABLE IF NOT EXISTS pages (
    book_id TEXT, page_no INTEGER, chapter_idx INTEGER, text TEXT, embedding BLOB,
    PRIMARY KEY (book_id, page_no)
);
CREATE TABLE IF NOT EXISTS chunks (
    book_id TEXT, idx INTEGER, start_page INTEGER, end_page INTEGER,
    done INTEGER DEFAULT 0, raw TEXT,
    PRIMARY KEY (book_id, idx)
);
CREATE TABLE IF NOT EXISTS entities (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    book_id TEXT, name TEXT, type TEXT, type_votes TEXT DEFAULT '{}',
    first_page INTEGER, mention_count INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS ix_entities_book ON entities(book_id);
CREATE TABLE IF NOT EXISTS aliases (
    entity_id INTEGER, alias TEXT, norm TEXT, first_page INTEGER,
    PRIMARY KEY (entity_id, norm)
);
CREATE INDEX IF NOT EXISTS ix_aliases_norm ON aliases(norm);
CREATE TABLE IF NOT EXISTS facts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    entity_id INTEGER, book_id TEXT, page INTEGER, text TEXT
);
CREATE INDEX IF NOT EXISTS ix_facts_entity ON facts(entity_id, page);
CREATE TABLE IF NOT EXISTS fact_links (
    fact_id INTEGER, entity_id INTEGER, PRIMARY KEY (fact_id, entity_id)
);
CREATE TABLE IF NOT EXISTS alias_mentions (      -- per alias, so later-revealed names don't leak
    entity_id INTEGER, norm TEXT, page INTEGER, count INTEGER, PRIMARY KEY (entity_id, norm, page)
);
CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS entity_summaries (
    entity_id INTEGER, sig TEXT, text TEXT, PRIMARY KEY (entity_id, sig)  -- sig = exact inputs used
);
"""

_local = threading.local()


def conn() -> sqlite3.Connection:
    c = getattr(_local, "conn", None)
    if c is None:
        c = sqlite3.connect(DB_PATH, timeout=30, check_same_thread=False)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA journal_mode=WAL")
        c.execute("PRAGMA foreign_keys=OFF")
        _local.conn = c
    return c


@contextmanager
def tx():
    c = conn()
    try:
        yield c
        c.commit()
    except Exception:
        c.rollback()
        raise


def init_db():
    c = conn()
    c.executescript(SCHEMA)
    c.commit()


def rows(sql, params=()):
    return [dict(r) for r in conn().execute(sql, params).fetchall()]


def row(sql, params=()):
    r = conn().execute(sql, params).fetchone()
    return dict(r) if r else None


def delete_book(book_id: str):
    with tx() as c:
        ids = [r[0] for r in c.execute("SELECT id FROM entities WHERE book_id=?", (book_id,))]
        clear_entities(c, ids)
        for t in ("chapters", "pages", "chunks"):
            c.execute(f"DELETE FROM {t} WHERE book_id=?", (book_id,))
        c.execute("DELETE FROM books WHERE id=?", (book_id,))
        c.execute("DELETE FROM kv WHERE key=?", (f"dedupe:{book_id}",))


def clear_entities(c, entity_ids):
    for eid in entity_ids:
        c.execute("DELETE FROM fact_links WHERE fact_id IN (SELECT id FROM facts WHERE entity_id=?)", (eid,))
        c.execute("DELETE FROM fact_links WHERE entity_id=?", (eid,))
        for t in ("aliases", "facts", "alias_mentions", "entity_summaries"):
            c.execute(f"DELETE FROM {t} WHERE entity_id=?", (eid,))
        c.execute("DELETE FROM entities WHERE id=?", (eid,))
