"""Book ingestion and codex extraction.

Pipeline per book:
  1. parse -> chapters -> fixed pages (stored)
  2. embed every page on the GPU (semantic search / Q&A)
  3. walk the book front-to-back in chunks; the LLM extracts book-specific entities with page-tagged facts
  4. merge each chunk's entities into the codex (alias + fuzzy resolution)
  5. index every mention of every alias across all pages, choose canonical names

Processing runs in page order, so the spoiler-free codex becomes usable for the first pages
long before the whole book is done.
"""
import json
import logging
import queue
import re
import threading
import traceback
from pathlib import Path

from rapidfuzz import fuzz

from . import db, rag
from .config import book_path, load_settings
from .llm import LLM, LLMError
from .parsers import paginate, parse_book

log = logging.getLogger("lorekeeper.extract")

TYPES = ["character", "place", "object", "event", "lore", "history", "faction", "creature", "other"]

SCHEMA = {
    "type": "object",
    "properties": {
        "entities": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "type": {"type": "string", "enum": TYPES},
                    "aliases": {"type": "array", "items": {"type": "string"}},
                    "facts": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "page": {"type": "integer"},
                                "text": {"type": "string"},
                                "related": {"type": "array", "items": {"type": "string"}},
                            },
                            "required": ["page", "text", "related"],
                        },
                    },
                },
                "required": ["name", "type", "aliases", "facts"],
            },
        }
    },
    "required": ["entities"],
}

SYSTEM = """You are a meticulous literary analyst building a spoiler-aware encyclopedia (codex) for a \
fantasy, science-fiction or fiction novel. You read one passage at a time and record what is UNIQUE \
to this book's world.

Extract every book-specific entity that appears in the passage:
- character: an INDIVIDUAL person or being - named people, wizards, witches, named animals, talking \
creatures, gods, AIs; also important unnamed individuals referred to by a distinctive title \
(e.g. "the Witch of the North").
- place: invented or story-specific locations, realms, cities, planets, ships used as places, buildings.
- object: named or special artifacts, weapons, relics, devices, magical items, special substances.
- event: notable in-story happenings (battles, rituals, journeys, deaths, discoveries) that have a name \
or are clearly significant plot events.
- lore: magic systems, technologies, religions, prophecies, customs, languages, laws of the world.
- history: the world's past - ages, ancient wars, dynasties, backstory told in the passage.
- faction: groups, peoples, nations, orders, houses, guilds.
- creature: a SPECIES or KIND of being (invented animals, monsters, races) - never an individual.
- other: anything else unique to this world.

Do NOT extract: ordinary things and common nouns (a house, a kiss, colours, weather, food, generic \
swords, horses, doors, "the forest"), real-world countries/cities unless they play a specific role in \
the story, pronouns, or the author. An ordinary object only counts if it is special in the story \
(magical, named, plot-critical).

For each entity give:
- name: its most complete proper name as used in the book.
- aliases: other names, nicknames, titles or epithets that the passage uses for the SAME entity.
- facts: short, self-contained statements (max ~30 words each) of what THIS passage states or shows \
about the entity: description, appearance, personality, abilities, possessions, relationships, goals, \
actions, what happens to it, history. Each fact is written so it makes sense on its own (use names, not \
pronouns). `page` is the [[PAGE n]] marker the information comes from. `related` lists the names of \
other extracted entities the fact involves.

Only use information in the passage. Never use outside knowledge about the book, even if you recognize \
it - later events must not leak into earlier pages. Prefer the entity names from the "Known entities" \
list when the passage refers to the same entity, so names stay consistent. Limit to the 30 most \
important entities and at most 8 facts per entity; skip entities that have nothing noteworthy."""

PRONOUNS = {"he", "she", "it", "they", "him", "her", "them", "i", "me", "we", "us", "you", "his",
            "hers", "its", "their", "the man", "the woman", "the girl", "the boy", "narrator"}
TITLES = {"mr", "mrs", "ms", "miss", "sir", "lady", "lord", "dr", "doctor", "captain", "king", "queen",
          "prince", "princess", "master", "the", "of", "a", "an", "saint", "st"}


def norm(name: str) -> str:
    n = name.lower().strip()
    n = re.sub(r"[’`]", "'", n)
    n = re.sub(r"'s$", "", n)
    n = re.sub(r"^(the|a|an)\s+", "", n)
    n = re.sub(r"[^\w\s'-]", "", n)
    return re.sub(r"\s+", " ", n).strip()


TYPE_SUFFIX_RE = re.compile(r"\s*[\(\[](?:%s)[\)\]]\s*$" % "|".join(TYPES), re.I)


def clean_name(name) -> str:
    """Strip artifacts the model copies from the prompt, e.g. "Scarecrow (creature)"."""
    if not isinstance(name, str):
        return ""
    return TYPE_SUFFIX_RE.sub("", name).strip().strip("\"'“”").strip()


def core_tokens(name: str) -> set:
    return {t for t in norm(name).replace("-", " ").split() if t not in TITLES and len(t) > 1}


# ------------------------------------------------------------------------------------------ ingest

def ingest(book_id: str, path: Path):
    s = load_settings()
    parsed = parse_book(path)
    pages = paginate(parsed.chapters, s["page_chars"])
    with db.tx() as c:
        c.execute("DELETE FROM pages WHERE book_id=?", (book_id,))
        c.execute("DELETE FROM chapters WHERE book_id=?", (book_id,))
        c.execute("DELETE FROM chunks WHERE book_id=?", (book_id,))
        first_page = {}
        for i, (ci, text) in enumerate(pages, start=1):
            first_page.setdefault(ci, i)
            c.execute("INSERT INTO pages(book_id, page_no, chapter_idx, text) VALUES (?,?,?,?)",
                      (book_id, i, ci, text))
        for ci, ch in enumerate(parsed.chapters):
            if ci in first_page:
                c.execute("INSERT INTO chapters VALUES (?,?,?,?)", (book_id, ci, ch.title, first_page[ci]))
        # Chunks for LLM extraction: consecutive pages up to chunk_chars.
        idx, start, size = 0, 1, 0
        for i, (_, text) in enumerate(pages, start=1):
            if size and size + len(text) > s["chunk_chars"]:
                c.execute("INSERT INTO chunks(book_id, idx, start_page, end_page) VALUES (?,?,?,?)",
                          (book_id, idx, start, i - 1))
                idx, start, size = idx + 1, i, 0
            size += len(text)
        c.execute("INSERT INTO chunks(book_id, idx, start_page, end_page) VALUES (?,?,?,?)",
                  (book_id, idx, start, len(pages)))
        c.execute("UPDATE books SET title=?, author=?, num_pages=? WHERE id=?",
                  (parsed.title, parsed.author, len(pages), book_id))


# ------------------------------------------------------------------------------------------ codex store

class Codex:
    """In-memory alias index for one book, backed by the DB."""

    def __init__(self, book_id: str):
        self.book_id = book_id
        self.alias_to_id = {}
        self.entities = {}
        for e in db.rows("SELECT id, name, type, type_votes, first_page FROM entities WHERE book_id=?",
                         (book_id,)):
            e["type_votes"] = json.loads(e["type_votes"] or "{}")
            e["fact_count"] = 0
            self.entities[e["id"]] = e
        for a in db.rows("SELECT a.entity_id, a.norm FROM aliases a JOIN entities e ON e.id=a.entity_id "
                         "WHERE e.book_id=?", (book_id,)):
            self.alias_to_id[a["norm"]] = a["entity_id"]
        for r in db.rows("SELECT entity_id, COUNT(*) n FROM facts WHERE book_id=? GROUP BY entity_id",
                         (book_id,)):
            if r["entity_id"] in self.entities:
                self.entities[r["entity_id"]]["fact_count"] = r["n"]

    def is_chapter_title(self, name: str) -> bool:
        if not hasattr(self, "_chapter_titles"):
            self._chapter_titles = {norm(r["title"]) for r in db.rows(
                "SELECT title FROM chapters WHERE book_id=?", (self.book_id,))}
        return norm(name) in self._chapter_titles or bool(re.match(r"(?i)^chapter\s+[\divxlc]+\b", name))

    def known_list(self, limit=150) -> str:
        ents = sorted(self.entities.values(), key=lambda e: -e["fact_count"])[:limit]
        if not ents:
            return "(none yet)"
        alias_map = {}
        for n, eid in self.alias_to_id.items():
            alias_map.setdefault(eid, []).append(n)
        lines = []
        for e in ents:
            others = [a for a in alias_map.get(e["id"], []) if a != norm(e["name"])][:3]
            lines.append(f"- {e['name']} ({e['type']})" + (f" aka {', '.join(others)}" if others else ""))
        return "\n".join(lines)

    def resolve(self, name: str, aliases: list, etype: str):
        """Find the existing entity for an extraction. The name is authoritative; aliases are only used
        when the name is new and they point at exactly one entity (e.g. "Strider" -> "Aragorn")."""
        eid = self.alias_to_id.get(norm(name))
        if eid:
            return eid
        toks = core_tokens(name)
        hits = {self.alias_to_id[norm(a)] for a in aliases if norm(a) in self.alias_to_id}
        if len(hits) == 1:
            eid = hits.pop()
            return None if self._conflicts(eid, toks) else eid
        # Fuzzy: partial-name match for characters ("Dorothy" vs "Dorothy Gale"), near-identical spellings.
        cands = set()
        for alias_norm, eid in self.alias_to_id.items():
            e = self.entities.get(eid)
            if not e:
                continue
            same_type = e["type"] == etype
            if same_type and len(alias_norm) >= 5 and fuzz.ratio(alias_norm, norm(name)) >= 92:
                cands.add(eid)
            elif same_type and etype == "character" and toks:
                other = core_tokens(alias_norm)
                if other and (toks <= other or other <= toks) and max(map(len, toks & other), default=0) >= 3:
                    cands.add(eid)
        if len(cands) != 1:
            return None
        eid = cands.pop()
        return None if self._conflicts(eid, toks) else eid

    def _conflicts(self, eid: int, toks: set) -> bool:
        """True if the entity already carries a fuller name that contradicts `toks`
        ("Wicked Witch of the East" must not absorb "Wicked Witch of the West")."""
        for alias_norm, other in self.alias_to_id.items():
            if other == eid:
                o = core_tokens(alias_norm)
                if o & toks and not (o <= toks or toks <= o):
                    return True
        return False

    def merge(self, c, keep: int, drop: int):
        if keep == drop or drop not in self.entities:
            return
        c.execute("UPDATE OR IGNORE aliases SET entity_id=? WHERE entity_id=?", (keep, drop))
        c.execute("DELETE FROM aliases WHERE entity_id=?", (drop,))
        c.execute("UPDATE facts SET entity_id=? WHERE entity_id=?", (keep, drop))
        c.execute("UPDATE OR IGNORE fact_links SET entity_id=? WHERE entity_id=?", (keep, drop))
        c.execute("DELETE FROM fact_links WHERE entity_id=?", (drop,))
        c.execute("DELETE FROM fact_links WHERE entity_id=? AND fact_id IN "
                  "(SELECT id FROM facts WHERE entity_id=?)", (keep, keep))
        c.execute("UPDATE OR IGNORE alias_mentions SET entity_id=? WHERE entity_id=?", (keep, drop))
        c.execute("DELETE FROM alias_mentions WHERE entity_id=?", (drop,))
        c.execute("DELETE FROM entity_summaries WHERE entity_id IN (?,?)", (keep, drop))
        c.execute("DELETE FROM entities WHERE id=?", (drop,))
        k, d = self.entities[keep], self.entities.pop(drop)
        for t, v in d["type_votes"].items():
            k["type_votes"][t] = k["type_votes"].get(t, 0) + v
        k["fact_count"] += d["fact_count"]
        k["first_page"] = min(k["first_page"], d["first_page"])
        for n, eid in list(self.alias_to_id.items()):
            if eid == drop:
                self.alias_to_id[n] = keep

    def add_chunk(self, extracted: list, start_page: int, end_page: int):
        pending_links = []
        chunk_names = {norm(clean_name(e.get("name"))) for e in extracted}
        with db.tx() as c:
            for ent in extracted:
                name = clean_name(ent.get("name"))
                if not name or norm(name) in PRONOUNS or len(norm(name)) < 2 or self.is_chapter_title(name):
                    continue
                etype = ent.get("type") if ent.get("type") in TYPES else "other"
                facts = [f for f in ent.get("facts") or [] if (f.get("text") or "").strip()]
                if not facts:
                    continue
                for f in facts:
                    try:
                        p = int(f.get("page") or start_page)
                    except (TypeError, ValueError):
                        p = start_page
                    f["page"] = min(max(p, start_page), end_page)
                # An alias that is another entity's name in this same passage is a model mix-up.
                aliases = [a for a in map(clean_name, ent.get("aliases") or []) if a
                           and (norm(a) == norm(name) or norm(a) not in chunk_names)]
                first = min(f["page"] for f in facts)

                eid = self.resolve(name, aliases, etype)
                if eid is None:
                    cur = c.execute("INSERT INTO entities(book_id, name, type, type_votes, first_page) "
                                    "VALUES (?,?,?,?,?)", (self.book_id, name, etype, "{}", first))
                    eid = cur.lastrowid
                    self.entities[eid] = {"id": eid, "name": name, "type": etype, "type_votes": {},
                                          "first_page": first, "fact_count": 0}
                e = self.entities[eid]
                e["type_votes"][etype] = e["type_votes"].get(etype, 0) + 1
                e["type"] = max(e["type_votes"], key=e["type_votes"].get)
                e["first_page"] = min(e["first_page"], first)
                for n in [name, *aliases]:
                    nn = norm(n)
                    if not nn or nn in PRONOUNS or len(n) > 80:
                        continue
                    if nn not in self.alias_to_id:
                        self.alias_to_id[nn] = eid
                    if self.alias_to_id[nn] == eid:
                        c.execute("INSERT OR IGNORE INTO aliases(entity_id, alias, norm, first_page) VALUES (?,?,?,?)",
                                  (eid, n, nn, first))
                        c.execute("UPDATE aliases SET first_page=MIN(first_page, ?) WHERE entity_id=? AND norm=?",
                                  (first, eid, nn))
                for f in facts:
                    cur = c.execute("INSERT INTO facts(entity_id, book_id, page, text) VALUES (?,?,?,?)",
                                    (eid, self.book_id, f["page"], f["text"].strip()))
                    e["fact_count"] += 1
                    for r in f.get("related") or []:
                        if isinstance(r, str):
                            pending_links.append((cur.lastrowid, eid, r))
                c.execute("UPDATE entities SET type=?, type_votes=?, first_page=? WHERE id=?",
                          (e["type"], json.dumps(e["type_votes"]), e["first_page"], eid))
            for fact_id, owner, rname in pending_links:
                target = self.alias_to_id.get(norm(rname))
                if target and target != owner:
                    c.execute("INSERT OR IGNORE INTO fact_links VALUES (?,?)", (fact_id, target))


# ------------------------------------------------------------------------------------------ mentions

def index_mentions(book_id: str, final: bool = False):
    """Count every occurrence of each alias per page. On `final`, choose canonical names and prune."""
    aliases = db.rows("SELECT a.entity_id, a.alias, a.norm FROM aliases a JOIN entities e ON e.id=a.entity_id "
                      "WHERE e.book_id=?", (book_id,))
    by_text = {}
    for a in aliases:
        text = re.sub(r"^(the|a|an)\s+", "", a["alias"].strip(), flags=re.I)
        # Only proper-looking names: capitalised or multi-word; avoids counting common words.
        if len(text) < 3 or not (text[:1].isupper() or " " in text):
            continue
        by_text.setdefault(text, a)
    counts, per_alias = {}, {}
    if by_text:
        keys = sorted(by_text, key=len, reverse=True)
        rx = re.compile(r"(?<![\w])(" + "|".join(re.escape(k) for k in keys) + r")(?![\w])")
        for p in db.rows("SELECT page_no, text FROM pages WHERE book_id=?", (book_id,)):
            for m in rx.finditer(p["text"]):
                a = by_text[m.group(1)]
                key = (a["entity_id"], a["norm"], p["page_no"])
                counts[key] = counts.get(key, 0) + 1
                per_alias[(a["entity_id"], a["alias"])] = per_alias.get((a["entity_id"], a["alias"]), 0) + 1
    with db.tx() as c:
        c.execute("DELETE FROM alias_mentions WHERE entity_id IN (SELECT id FROM entities WHERE book_id=?)", (book_id,))
        c.executemany("INSERT INTO alias_mentions VALUES (?,?,?,?)", [(*k, n) for k, n in counts.items()])
        c.execute("UPDATE entities SET mention_count=(SELECT COALESCE(SUM(count),0) FROM alias_mentions m "
                  "WHERE m.entity_id=entities.id) WHERE book_id=?", (book_id,))
        if final:
            orphan = [r[0] for r in c.execute(
                "SELECT e.id FROM entities e WHERE e.book_id=? AND NOT EXISTS "
                "(SELECT 1 FROM facts f WHERE f.entity_id=e.id)", (book_id,))]
            db.clear_entities(c, orphan)
            # Canonical name: the alias used most often in the text (ties -> longer, more complete name).
            # Spoiler mode still shows the earliest-known alias until this one has been read.
            best = {}
            for (eid, alias), n in per_alias.items():
                if eid not in best or (n, len(alias)) > best[eid][0]:
                    best[eid] = ((n, len(alias)), alias)
            for eid, (_, alias) in best.items():
                c.execute("UPDATE entities SET name=? WHERE id=?", (alias, eid))


# ------------------------------------------------------------------------------------------ cleanup

SAME_SCHEMA = {
    "type": "object",
    "properties": {"same": {"type": "boolean"}, "confidence": {"type": "string", "enum": ["low", "medium", "high"]}},
    "required": ["same", "confidence"],
}
SAME_SYSTEM = """You check a novel's codex for duplicate entries. Two entries are the SAME only if they \
are the same individual person, place or thing referred to by different names (e.g. "Emerald City" and \
"The City of Emeralds"; a nickname and a full name). They are DIFFERENT if one is a group and the other \
a member, one is a place and the other something in or near it, one is an object and the other an \
event, or they are merely related. When in doubt, answer false."""

TYPE_FAMILIES = [("character", "creature", "faction"), ("place",), ("object", "event", "lore", "history", "other")]


def dedupe(book_id: str, llm: LLM, max_pairs: int = 60):
    """Embedding similarity proposes candidate pairs; the LLM verifies each pair on its own."""
    codex = Codex(book_id)
    facts, aliases = {}, {}
    for f in db.rows("SELECT entity_id, text FROM facts WHERE book_id=? ORDER BY page, id", (book_id,)):
        facts.setdefault(f["entity_id"], []).append(f["text"])
    for a in db.rows("SELECT a.entity_id, a.alias FROM aliases a JOIN entities e ON e.id=a.entity_id "
                     "WHERE e.book_id=?", (book_id,)):
        aliases.setdefault(a["entity_id"], []).append(a["alias"])
    checked = {tuple(p) for p in json.loads((db.row("SELECT value FROM kv WHERE key=?", (f"dedupe:{book_id}",))
                                             or {"value": "[]"})["value"])}
    pairs = []
    for family in TYPE_FAMILIES:
        ents = sorted((e for e in codex.entities.values() if e["type"] in family),
                      key=lambda e: -e["fact_count"])[:500]
        if len(ents) < 2:
            continue
        labels = [re.sub(r"^(the|a|an)\s+", "", e["name"], flags=re.I) for e in ents]
        try:
            vecs = rag.embed(labels).astype("float32")
        except Exception as e:  # noqa: BLE001
            log.warning("dedupe embeddings failed: %s", e)
            return
        sims = vecs @ vecs.T
        for i in range(len(ents)):
            for j in range(i + 1, len(ents)):
                a, b = ents[i]["id"], ents[j]["id"]
                key = (min(a, b), max(a, b))
                lexical = bool(core_tokens(labels[i]) & core_tokens(labels[j]))
                if key not in checked and (sims[i, j] >= 0.80 or (lexical and sims[i, j] >= 0.70)):
                    pairs.append((float(sims[i, j]), key))
    pairs.sort(reverse=True)

    def describe(eid):
        e = codex.entities[eid]
        aka = [a for a in aliases.get(eid, []) if norm(a) != norm(e["name"])][:5]
        notes = "\n".join(f"  - {t}" for t in (facts.get(eid) or [])[:6])
        return f"{e['name']} ({e['type']})" + (f", also called {', '.join(aka)}" if aka else "") + f"\n{notes}"

    for _, (a, b) in pairs[:max_pairs]:
        checked.add((a, b))
        if a not in codex.entities or b not in codex.entities:
            continue
        try:
            out = llm.json(SAME_SYSTEM, f"Entry A: {describe(a)}\n\nEntry B: {describe(b)}\n\n"
                                        "Are A and B the same entity?", SAME_SCHEMA, max_tokens=2000,
                           think=True)
        except LLMError as e:
            log.warning("dedupe check failed: %s", e)
            continue
        if out.get("same") is True and out.get("confidence") == "high":
            keep, drop = sorted((a, b), key=lambda i: -codex.entities[i]["fact_count"])
            log.info("merge %s <- %s", codex.entities[keep]["name"], codex.entities[drop]["name"])
            with db.tx() as c:
                codex.merge(c, keep, drop)
                k = codex.entities[keep]
                k["type"] = max(k["type_votes"], key=k["type_votes"].get) if k["type_votes"] else k["type"]
                c.execute("UPDATE entities SET type=?, type_votes=?, first_page=? WHERE id=?",
                          (k["type"], json.dumps(k["type_votes"]), k["first_page"], keep))
    with db.tx() as c:
        c.execute("INSERT OR REPLACE INTO kv VALUES (?,?)", (f"dedupe:{book_id}", json.dumps(sorted(checked))))


def prune_generic(book_id: str):
    """Drop minor common-noun entries ("kiss", "the house") that the model extracted despite instructions."""
    with db.tx() as c:
        drop = []
        for e in c.execute("SELECT e.id, e.name, e.type, (SELECT COUNT(*) FROM facts f WHERE f.entity_id=e.id) n "
                           "FROM entities e WHERE e.book_id=?", (book_id,)).fetchall():
            names = [e["name"], *(r[0] for r in c.execute("SELECT alias FROM aliases WHERE entity_id=?", (e["id"],)))]
            proper = any(re.sub(r"^(the|a|an)\s+", "", n.strip(), flags=re.I)[:1].isupper() for n in names)
            single_common = not proper and " " not in e["name"].strip() and not re.match(r"(?i)the\s", e["name"])
            if e["type"] != "character" and not proper and (e["n"] <= 4 or (single_common and e["n"] <= 10)):
                drop.append(e["id"])
        db.clear_entities(c, drop)


# ------------------------------------------------------------------------------------------ worker

def chunk_text(book_id: str, start: int, end: int) -> str:
    pages = db.rows("SELECT page_no, text FROM pages WHERE book_id=? AND page_no BETWEEN ? AND ? "
                    "ORDER BY page_no", (book_id, start, end))
    return "\n\n".join(f"[[PAGE {p['page_no']}]]\n{p['text']}" for p in pages)


def extract_range(llm: LLM, codex: Codex, book_id: str, start: int, end: int, depth: int = 0) -> list:
    user = (f"Known entities so far:\n{codex.known_list()}\n\n"
            f"Passage (pages {start}-{end}):\n\"\"\"\n{chunk_text(book_id, start, end)}\n\"\"\"\n\n"
            "Return the JSON codex entries for this passage.")
    try:
        out = llm.json(SYSTEM, user, SCHEMA, max_tokens=6000, retries=1)
        return out.get("entities") or []
    except LLMError:
        if end > start and depth < 3:  # probably output overflow: split the passage and retry
            mid = (start + end) // 2
            return (extract_range(llm, codex, book_id, start, mid, depth + 1)
                    + extract_range(llm, codex, book_id, mid + 1, end, depth + 1))
        raise


HIDDEN_TOC = ("The printed table of contents is hidden because chapter titles can give away the story. "
              "Use the chapter menu at the top instead.")


def clean_front_matter(book_id: str):
    """For books imported before the parser dropped contents/copyright pages: hide the table of contents
    (text + search index) and remove codex entries that only came from publisher pages. Page numbers stay
    the same, so nothing needs re-analysing. Safe to run repeatedly."""
    from .parsers import Chapter, is_front_matter, is_toc

    chapters = db.rows("SELECT idx, title FROM chapters WHERE book_id=? ORDER BY start_page", (book_id,))
    pages_by_ch = {}
    for p in db.rows("SELECT page_no, chapter_idx, text FROM pages WHERE book_id=? ORDER BY page_no", (book_id,)):
        pages_by_ch.setdefault(p["chapter_idx"], []).append(p)
    junk_pages, changed = [], False
    for ch in chapters:
        pages = pages_by_ch.get(ch["idx"], [])
        if not pages or any(HIDDEN_TOC in p["text"] for p in pages):
            continue
        c = Chapter(ch["title"] or "", [x for p in pages for x in p["text"].split("\n\n")])
        if is_toc(c):
            with db.tx() as tx:
                for p in pages:
                    tx.execute("UPDATE pages SET text=?, embedding=NULL WHERE book_id=? AND page_no=?",
                               (f"# Contents\n\n{HIDDEN_TOC}", book_id, p["page_no"]))
            junk_pages += [p["page_no"] for p in pages]
            changed = True
        elif is_front_matter(c):
            junk_pages += [p["page_no"] for p in pages]
    if junk_pages:
        marks = ",".join("?" * len(junk_pages))
        with db.tx() as tx:
            fact_ids = [r[0] for r in tx.execute(
                f"SELECT id FROM facts WHERE book_id=? AND page IN ({marks})", (book_id, *junk_pages))]
            if fact_ids:
                changed = True
                fm = ",".join("?" * len(fact_ids))
                tx.execute(f"DELETE FROM fact_links WHERE fact_id IN ({fm})", fact_ids)
                tx.execute(f"DELETE FROM facts WHERE id IN ({fm})", fact_ids)
                orphans = [r[0] for r in tx.execute(
                    "SELECT e.id FROM entities e WHERE e.book_id=? AND NOT EXISTS "
                    "(SELECT 1 FROM facts f WHERE f.entity_id=e.id)", (book_id,))]
                db.clear_entities(tx, orphans)
                tx.execute("UPDATE entities SET first_page=(SELECT MIN(page) FROM facts f WHERE f.entity_id=entities.id) "
                           "WHERE book_id=?", (book_id,))
                log.info("front matter cleanup for %s: removed %d facts, %d entries", book_id, len(fact_ids), len(orphans))
    if changed:
        index_mentions(book_id)


def rebuild_from_raw(book_id: str):
    """Re-run entity resolution over the stored raw LLM outputs (no new LLM extraction calls)."""
    with db.tx() as c:
        db.clear_entities(c, [r[0] for r in c.execute("SELECT id FROM entities WHERE book_id=?", (book_id,))])
        c.execute("DELETE FROM kv WHERE key=?", (f"dedupe:{book_id}",))
    codex = Codex(book_id)
    for ch in db.rows("SELECT * FROM chunks WHERE book_id=? AND done=1 ORDER BY idx", (book_id,)):
        codex.add_chunk(json.loads(ch["raw"] or "[]"), ch["start_page"], ch["end_page"])
    clean_front_matter(book_id)


def set_status(book_id: str, **kw):
    cols = ", ".join(f"{k}=?" for k in kw)
    with db.tx() as c:
        c.execute(f"UPDATE books SET {cols} WHERE id=?", (*kw.values(), book_id))


def process_book(book_id: str):
    book = db.row("SELECT * FROM books WHERE id=?", (book_id,))
    if not book:
        return
    if not book["num_pages"]:
        set_status(book_id, status="processing", stage="Parsing book", progress=0, error=None)
        ingest(book_id, book_path(book["file_path"]))

    set_status(book_id, status="processing", stage="Embedding pages (GPU)", error=None)
    try:
        rag.embed_book(book_id)
    except Exception as e:  # search is optional; extraction can proceed
        log.warning("embedding failed: %s", e)
    try:
        import torch
        torch.cuda.empty_cache()  # leave VRAM for the LLM
    except Exception:
        pass

    llm = LLM()
    codex = Codex(book_id)
    chunks = db.rows("SELECT * FROM chunks WHERE book_id=? ORDER BY idx", (book_id,))
    total = len(chunks)
    for n, ch in enumerate(chunks):
        if ch["done"]:
            continue
        status = db.row("SELECT status FROM books WHERE id=?", (book_id,))
        if not status or status["status"] != "processing":
            return  # paused or deleted
        set_status(book_id, stage=f"Reading pages {ch['start_page']}-{ch['end_page']} ({llm.label})",
                   progress=n / total)
        entities = extract_range(llm, codex, book_id, ch["start_page"], ch["end_page"])
        status = db.row("SELECT status FROM books WHERE id=?", (book_id,))
        if not status or status["status"] != "processing":
            return  # paused/reset/deleted while the model was running: discard this chunk
        codex.add_chunk(entities, ch["start_page"], ch["end_page"])
        with db.tx() as c:
            c.execute("UPDATE chunks SET done=1, raw=? WHERE book_id=? AND idx=?",
                      (json.dumps(entities), book_id, ch["idx"]))
            c.execute("UPDATE books SET processed_page=?, progress=? WHERE id=?",
                      (ch["end_page"], (n + 1) / total, book_id))
        if n % 16 == 15:
            set_status(book_id, stage="Merging duplicate entries")
            dedupe(book_id, llm)
            codex = Codex(book_id)
        if n % 8 == 7:
            index_mentions(book_id)
    set_status(book_id, stage="Merging duplicate entries")
    dedupe(book_id, llm)
    prune_generic(book_id)
    set_status(book_id, stage="Indexing mentions")
    index_mentions(book_id, final=True)
    set_status(book_id, status="done", stage="", progress=1.0,
               processed_page=db.row("SELECT num_pages FROM books WHERE id=?", (book_id,))["num_pages"])


class Worker:
    def __init__(self):
        self.q = queue.Queue()
        self.current = None
        threading.Thread(target=self._run, daemon=True, name="extractor").start()

    def enqueue(self, book_id: str):
        self.q.put(book_id)

    def _run(self):
        while True:
            book_id = self.q.get()
            self.current = book_id
            try:
                book = db.row("SELECT status FROM books WHERE id=?", (book_id,))
                if book and book["status"] in ("queued", "processing"):
                    set_status(book_id, status="processing")
                    process_book(book_id)
            except Exception as e:  # noqa: BLE001
                traceback.print_exc()
                if db.row("SELECT id FROM books WHERE id=?", (book_id,)):
                    set_status(book_id, status="error", error=str(e)[:500])
            finally:
                self.current = None
