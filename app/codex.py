"""Spoiler-aware codex queries. Everything takes `upto` = last page the reader is allowed to know about."""
import hashlib
import re

from . import db, rag
from .extraction import TYPES, norm
from .llm import LLM

SUMMARY_SYSTEM = """You write encyclopedia entries for a book companion app. You are given notes about one \
entity from a novel, each tagged with the page it comes from. Write a well-organised entry in Markdown \
(short intro paragraph, then sections such as Appearance, Personality, Abilities, History, Relationships, \
Role in the story - only those that the notes support).

STRICT RULES:
- Use ONLY the information in the notes. Do not add anything from your own knowledge of this book, its \
sequels or adaptations, even if you recognise it. The reader has only read up to the last page given, \
and any extra detail may be a spoiler.
- Do not speculate about the future or hint at what happens later.
- Where notes change over time, describe the latest state and mention earlier states briefly.
- Keep it under 350 words. Do not start with a title, heading or the entity's name on its own line."""

ASK_SYSTEM = """You answer questions about a novel for a reader who has only read up to page {upto}. \
Answer ONLY from the provided excerpts and codex notes, which all come from pages the reader has read. \
Never use outside knowledge of the book, its sequels or adaptations - that could spoil the story. \
If the excerpts don't contain the answer, say that the book hasn't revealed it yet (as far as the \
reader has read). Cite pages like [p. 12]. Be concise."""


def clamp_upto(book: dict, upto) -> int:
    n = book["num_pages"] or 1
    try:
        u = int(upto) if upto not in (None, "") else n
    except ValueError:
        u = n
    return max(1, min(u, n))


def _visible_names(entity_ids, upto):
    """Map entity id -> (display name, visible aliases) using only aliases first seen by `upto`."""
    if not entity_ids:
        return {}
    out = {}
    qmarks = ",".join("?" * len(entity_ids))
    ents = {e["id"]: e for e in db.rows(f"SELECT id, name FROM entities WHERE id IN ({qmarks})", entity_ids)}
    aliases = db.rows(f"SELECT entity_id, alias, first_page FROM aliases WHERE entity_id IN ({qmarks}) "
                      "AND first_page<=? ORDER BY first_page", (*entity_ids, upto))
    by_e = {}
    for a in aliases:
        by_e.setdefault(a["entity_id"], []).append(a["alias"])
    for eid, e in ents.items():
        visible = by_e.get(eid, [])
        canon_ok = any(norm(a) == norm(e["name"]) for a in visible)
        name = e["name"] if canon_ok else (visible[0] if visible else "(unnamed)")
        seen, alias_list = {norm(name)}, []
        for a in visible:
            if norm(a) not in seen:
                seen.add(norm(a))
                alias_list.append(a)
        out[eid] = (name, alias_list)
    return out


def list_entities(book_id: str, upto: int, etype: str = "", q: str = ""):
    rs = db.rows(
        """SELECT e.id, e.type, e.first_page,
                  (SELECT COUNT(*) FROM facts f WHERE f.entity_id=e.id AND f.page<=?) AS facts,
                  (SELECT COALESCE(SUM(m.count),0) FROM alias_mentions m JOIN aliases a
                     ON a.entity_id=m.entity_id AND a.norm=m.norm
                   WHERE m.entity_id=e.id AND m.page<=? AND a.first_page<=?) AS mentions
           FROM entities e WHERE e.book_id=? AND e.first_page<=?""",
        (upto, upto, upto, book_id, upto))
    rs = [r for r in rs if r["facts"] > 0]
    names = _visible_names([r["id"] for r in rs], upto)
    counts = {t: 0 for t in TYPES}
    out = []
    ql = q.lower().strip()
    for r in rs:
        r["name"], r["aliases"] = names.get(r["id"], ("?", []))
        counts[r["type"]] = counts.get(r["type"], 0) + 1
        if etype and r["type"] != etype:
            continue
        if ql and ql not in r["name"].lower() and not any(ql in a.lower() for a in r["aliases"]):
            continue
        r["score"] = r["facts"] * 2 + r["mentions"]
        out.append(r)
    out.sort(key=lambda r: -r["score"])
    return {"entities": out, "counts": counts}


def entity_detail(book_id: str, eid: int, upto: int):
    e = db.row("SELECT * FROM entities WHERE id=? AND book_id=?", (eid, book_id))
    if not e or e["first_page"] > upto:
        return None
    chapters = db.rows("SELECT idx, title, start_page FROM chapters WHERE book_id=? ORDER BY start_page", (book_id,))

    def chapter_of(page):
        title = ""
        for c in chapters:
            if c["start_page"] <= page:
                title = c["title"]
        return title

    facts = db.rows("SELECT id, page, text FROM facts WHERE entity_id=? AND page<=? ORDER BY page, id", (eid, upto))
    # Facts recorded on other entries that involve this one ("appears in").
    cross = db.rows(
        """SELECT f.id, f.page, f.text, f.entity_id FROM fact_links l JOIN facts f ON f.id=l.fact_id
           WHERE l.entity_id=? AND f.page<=? ORDER BY f.page""", (eid, upto))
    rel_counts = {}
    for r in db.rows("""SELECT l.entity_id AS other FROM fact_links l JOIN facts f ON f.id=l.fact_id
                        WHERE f.entity_id=? AND f.page<=?""", (eid, upto)):
        rel_counts[r["other"]] = rel_counts.get(r["other"], 0) + 1
    for r in cross:
        rel_counts[r["entity_id"]] = rel_counts.get(r["entity_id"], 0) + 1
    rel_ids = [i for i in rel_counts if i != eid]
    visible_rel = {r["id"]: r for r in db.rows(
        f"SELECT id, type, first_page FROM entities WHERE id IN ({','.join('?' * len(rel_ids))})", rel_ids)
    } if rel_ids else {}
    rel_ids = [i for i in rel_ids if i in visible_rel and visible_rel[i]["first_page"] <= upto]
    names = _visible_names([eid, *rel_ids], upto)
    related = sorted(({"id": i, "name": names[i][0], "type": visible_rel[i]["type"], "weight": rel_counts[i]}
                      for i in rel_ids if i in names), key=lambda r: -r["weight"])
    mentions = db.rows(
        """SELECT m.page, SUM(m.count) AS count FROM alias_mentions m JOIN aliases a
             ON a.entity_id=m.entity_id AND a.norm=m.norm
           WHERE m.entity_id=? AND m.page<=? AND a.first_page<=? GROUP BY m.page ORDER BY m.page""",
        (eid, upto, upto))
    for f in facts:
        f["chapter"] = chapter_of(f["page"])
    own_ids = {f["id"] for f in facts}
    cross_out = []
    for f in cross:
        if f["id"] in own_ids or f["entity_id"] not in names:
            continue
        cross_out.append({"page": f["page"], "text": f["text"], "entity_id": f["entity_id"],
                          "entity": names[f["entity_id"]][0]})
    name, aliases = names[eid]
    d = {"id": eid, "name": name, "aliases": aliases, "type": e["type"], "first_page": e["first_page"],
         "first_chapter": chapter_of(e["first_page"]), "facts": facts, "appears_in": cross_out,
         "related": related[:40], "mentions": mentions, "upto": upto}
    # The cache key is a hash of the exact prompt, so a summary written with later information
    # can never be served to a reader at an earlier page.
    d["_prompt"] = _summary_prompt(d)
    d["_sig"] = hashlib.sha1(d["_prompt"].encode("utf-8")).hexdigest()
    cached = db.row("SELECT text FROM entity_summaries WHERE entity_id=? AND sig=?", (eid, d["_sig"]))
    d["summary"] = cached["text"] if cached else None
    return d


def _summary_prompt(d: dict) -> str:
    lines = [f"[p. {f['page']}] {f['text']}" for f in d["facts"]]
    lines += [f"[p. {f['page']}] ({f['entity']}) {f['text']}" for f in d["appears_in"][:60]]
    if sum(len(x) for x in lines) > 16000:  # keep the start and the most recent notes
        lines = lines[:50] + ["..."] + lines[-150:]
    return (f"Entity: {d['name']} ({d['type']})" + (f", also called {', '.join(d['aliases'])}" if d["aliases"] else "")
            + "\n\nNotes:\n" + "\n".join(lines))


def public(d: dict) -> dict:
    return {k: v for k, v in d.items() if not k.startswith("_")}


def summarize(book_id: str, eid: int, upto: int, llm: LLM | None = None) -> str:
    d = entity_detail(book_id, eid, upto)
    if not d:
        raise ValueError("Entity not visible at this point in the book.")
    if d["summary"]:
        return d["summary"]
    text = (llm or LLM()).text(SUMMARY_SYSTEM, d["_prompt"], max_tokens=900)
    # Drop a leading title line such as "# Dorothy" or "**Overview**".
    lines = text.strip().split("\n")
    while lines and (not lines[0].strip() or (len(lines[0]) < 60 and re.match(r"^\s*(#+|\*\*)", lines[0]))):
        lines.pop(0)
    text = "\n".join(lines).strip() or text
    with db.tx() as c:
        c.execute("INSERT OR REPLACE INTO entity_summaries VALUES (?,?,?)", (eid, d["_sig"], text))
    return text


def highlights(book_id: str, upto: int):
    """Aliases the reader already knows, for underlining names in the page text."""
    ents = list_entities(book_id, upto)["entities"]
    out = []
    for e in ents:
        for a in [e["name"], *e["aliases"]]:
            a2 = re.sub(r"^(the|a|an)\s+", "", a, flags=re.I).strip()
            if len(a2) >= 3 and (a2[:1].isupper() or " " in a2):
                out.append({"text": a2, "id": e["id"], "type": e["type"]})
    return out


def ask(book_id: str, question: str, upto: int) -> dict:
    passages = []
    try:
        passages = rag.search(book_id, question, upto, k=6)
    except Exception:
        passages = []
    notes = []
    ql = question.lower()
    for e in list_entities(book_id, upto)["entities"][:400]:
        if any(len(n) >= 3 and n.lower() in ql for n in [e["name"], *e["aliases"]]):
            for f in db.rows("SELECT page, text FROM facts WHERE entity_id=? AND page<=? ORDER BY page",
                             (e["id"], upto))[-40:]:
                notes.append(f"[p. {f['page']}] ({e['name']}) {f['text']}")
    ctx = "\n\n".join(f"--- Excerpt [p. {p['page']}] ---\n{p['text']}" for p in sorted(passages, key=lambda p: p["page"]))
    user = f"Codex notes:\n" + ("\n".join(notes[:120]) or "(none)") + f"\n\nExcerpts:\n{ctx or '(none)'}\n\nQuestion: {question}"
    answer = LLM().text(ASK_SYSTEM.format(upto=upto), user, max_tokens=700)
    return {"answer": answer, "pages": sorted({p["page"] for p in passages})}
