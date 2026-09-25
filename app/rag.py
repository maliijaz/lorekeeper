"""Semantic page search with an open embedding model on the GPU (sentence-transformers)."""
import threading

import numpy as np

from . import db
from .config import load_settings

_model = None
_model_name = None
_lock = threading.Lock()
QUERY_PREFIX = "Represent this sentence for searching relevant passages: "


def model():
    global _model, _model_name
    name = load_settings()["embed_model"]
    with _lock:
        if _model is None or _model_name != name:
            import torch
            from sentence_transformers import SentenceTransformer

            device = "cuda" if torch.cuda.is_available() else "cpu"
            _model = SentenceTransformer(name, device=device)
            if device == "cuda":
                _model.half()
            _model_name = name
    return _model


def embed(texts, query=False) -> np.ndarray:
    m = model()
    if query and "bge" in (_model_name or "").lower():
        texts = [QUERY_PREFIX + t for t in texts]
    vecs = m.encode(texts, batch_size=32, normalize_embeddings=True, convert_to_numpy=True,
                    show_progress_bar=False)
    return vecs.astype(np.float16)


def embed_book(book_id: str, on_progress=None):
    pages = db.rows("SELECT page_no, text FROM pages WHERE book_id=? AND embedding IS NULL ORDER BY page_no",
                    (book_id,))
    step = 64
    for i in range(0, len(pages), step):
        batch = pages[i:i + step]
        vecs = embed([p["text"] for p in batch])
        with db.tx() as c:
            c.executemany("UPDATE pages SET embedding=? WHERE book_id=? AND page_no=?",
                          [(v.tobytes(), book_id, p["page_no"]) for v, p in zip(vecs, batch)])
        if on_progress:
            on_progress(min(1.0, (i + step) / max(1, len(pages))))


def search(book_id: str, query: str, max_page: int, k: int = 6):
    rs = db.rows("SELECT page_no, text, embedding FROM pages WHERE book_id=? AND page_no<=? "
                 "AND embedding IS NOT NULL", (book_id, max_page))
    if not rs:
        return []
    mat = np.stack([np.frombuffer(r["embedding"], dtype=np.float16) for r in rs]).astype(np.float32)
    q = embed([query], query=True)[0].astype(np.float32)
    scores = mat @ q
    top = np.argsort(-scores)[:k]
    return [{"page": rs[i]["page_no"], "text": rs[i]["text"], "score": float(scores[i])} for i in top]
