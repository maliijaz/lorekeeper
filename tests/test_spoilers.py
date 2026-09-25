"""Spoiler-shield regression tests. No LLM needed: entities are fed straight into the resolver.

Run:  python -m pytest tests -q
"""
import os
import sys
import tempfile
from pathlib import Path

os.environ["LOREKEEPER_DATA"] = tempfile.mkdtemp(prefix="sr_test_")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import codex, db  # noqa: E402
from app.extraction import Codex, index_mentions  # noqa: E402

BOOK = "test"
PAGES = {
    1: "A ranger called Strider sat in the corner of the Prancing Pony.",
    2: "Strider led the hobbits through the Midgewater Marshes.",
    3: "An old song spoke of Aragorn, heir of kings.",
    4: "Strider revealed that he was Aragorn son of Arathorn.",
    5: "Aragorn drew the sword Anduril at the Black Gate.",
}


def setup_module():
    db.init_db()
    with db.tx() as c:
        c.execute("INSERT INTO books(id, title, num_pages, status) VALUES (?,?,?,?)", (BOOK, "Test", 5, "done"))
        for n, t in PAGES.items():
            c.execute("INSERT INTO pages(book_id, page_no, chapter_idx, text) VALUES (?,?,0,?)", (BOOK, n, t))
        c.execute("INSERT INTO chapters VALUES (?,?,?,?)", (BOOK, 0, "Chapter One", 1))
    cx = Codex(BOOK)
    cx.add_chunk([
        {"name": "Strider", "type": "character", "aliases": [],
         "facts": [{"page": 1, "text": "Strider is a ranger at the Prancing Pony.", "related": ["Prancing Pony"]},
                   {"page": 2, "text": "Strider guides the hobbits.", "related": []}]},
        {"name": "Prancing Pony", "type": "place", "aliases": [],
         "facts": [{"page": 1, "text": "The Prancing Pony is an inn.", "related": []}]},
    ], 1, 2)
    cx.add_chunk([
        {"name": "Aragorn", "type": "character", "aliases": ["Strider"],
         "facts": [{"page": 4, "text": "Strider is really Aragorn, son of Arathorn.", "related": []}]},
    ], 3, 4)
    cx.add_chunk([
        {"name": "Anduril", "type": "object", "aliases": [],
         "facts": [{"page": 5, "text": "Anduril is Aragorn's sword.", "related": ["Aragorn"]}]},
        {"name": "Aragorn", "type": "character", "aliases": [],
         "facts": [{"page": 5, "text": "Aragorn fights at the Black Gate.", "related": ["Anduril"]}]},
    ], 5, 5)
    index_mentions(BOOK, final=True)


def names_at(upto):
    return {e["name"] for e in codex.list_entities(BOOK, upto)["entities"]}


def strider_id():
    return db.row("SELECT entity_id FROM aliases WHERE norm='strider'")["entity_id"]


def test_later_entities_hidden():
    assert "Anduril" not in names_at(4)
    assert "Anduril" in names_at(5)


def test_revealed_name_not_shown_early():
    # Canonical name becomes "Aragorn" (most mentions), but before page 4 the reader only knows "Strider".
    assert "Aragorn" not in names_at(3)
    assert "Strider" in names_at(3)
    d = codex.entity_detail(BOOK, strider_id(), 3)
    assert d["name"] == "Strider" and "Aragorn" not in d["aliases"]
    assert all("Aragorn" not in f["text"] for f in d["facts"])


def test_facts_and_links_capped():
    for upto in range(1, 6):
        d = codex.entity_detail(BOOK, strider_id(), upto)
        assert all(f["page"] <= upto for f in d["facts"])
        assert all(f["page"] <= upto for f in d["appears_in"])
        assert all(m["page"] <= upto for m in d["mentions"])
        visible = {e["id"] for e in codex.list_entities(BOOK, upto)["entities"]}
        assert all(r["id"] in visible for r in d["related"])


def test_mentions_of_unrevealed_alias_hidden():
    # Page 3 mentions "Aragorn", which is the same person, but the reader doesn't know that yet.
    d = codex.entity_detail(BOOK, strider_id(), 3)
    assert 3 not in {m["page"] for m in d["mentions"]}
    d5 = codex.entity_detail(BOOK, strider_id(), 5)
    assert 3 in {m["page"] for m in d5["mentions"]}


def test_summary_cache_key_depends_on_inputs():
    sigs = {codex.entity_detail(BOOK, strider_id(), u)["_sig"] for u in (2, 4, 5)}
    assert len(sigs) == 3
    assert codex.entity_detail(BOOK, strider_id(), 2)["_sig"] == codex.entity_detail(BOOK, strider_id(), 2)["_sig"]


def test_highlights_only_known_names():
    texts = {h["text"] for h in codex.highlights(BOOK, 3)}
    assert "Strider" in texts and "Aragorn" not in texts and "Anduril" not in texts


def test_alias_does_not_merge_distinct_named_entity():
    cx = Codex(BOOK)
    cx.add_chunk([
        {"name": "Anduril", "type": "object", "aliases": ["Aragorn"],  # model mix-up
         "facts": [{"page": 5, "text": "Anduril was reforged.", "related": []}]},
    ], 5, 5)
    anduril = db.row("SELECT entity_id FROM aliases WHERE norm='anduril'")["entity_id"]
    assert anduril != strider_id()
