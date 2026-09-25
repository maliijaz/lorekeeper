"""Runtime configuration. Values come from defaults, then data/settings.json, then env vars."""
import json
import os
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = Path(os.environ.get("LOREKEEPER_DATA") or os.environ.get("SMART_READER_DATA") or ROOT / "data")
BOOKS_DIR = DATA_DIR / "books"
DB_PATH = DATA_DIR / "lorekeeper.db"
LEGACY_DB_PATH = DATA_DIR / "smart_reader.db"  # name used before the app was renamed to Lorekeeper
SETTINGS_PATH = DATA_DIR / "settings.json"
STATIC_DIR = ROOT / "static"

DATA_DIR.mkdir(parents=True, exist_ok=True)
BOOKS_DIR.mkdir(parents=True, exist_ok=True)

if LEGACY_DB_PATH.exists() and not DB_PATH.exists():
    # Rename the database together with its WAL/SHM files (their names are tied to the DB name).
    for suffix in ("-wal", "-shm", ""):
        old = LEGACY_DB_PATH.with_name(LEGACY_DB_PATH.name + suffix)
        if old.exists():
            old.rename(DB_PATH.with_name(DB_PATH.name + suffix))

DEFAULTS = {
    # "ollama" = local GPU via Ollama; "openai" = any OpenAI-compatible server hosting open models
    # (Groq, Hugging Face router, vLLM, llama.cpp server, LM Studio ...).
    "provider": "ollama",
    "ollama_url": "http://127.0.0.1:11434",
    "ollama_model": "qwen3:8b",
    "ollama_num_ctx": 8192,
    "openai_base_url": "https://api.groq.com/openai/v1",
    "openai_api_key": "",
    "openai_model": "openai/gpt-oss-120b",
    # Sentence-transformers model for semantic search (runs on CUDA when available).
    "embed_model": "BAAI/bge-small-en-v1.5",
    "page_chars": 1800,     # characters per reader page (pages are the spoiler boundary unit)
    "chunk_chars": 6000,    # characters sent to the LLM per extraction call
}

ENV_MAP = {
    "provider": "SR_PROVIDER",
    "ollama_url": "SR_OLLAMA_URL",
    "ollama_model": "SR_OLLAMA_MODEL",
    "openai_base_url": "SR_OPENAI_BASE_URL",
    "openai_api_key": "SR_OPENAI_API_KEY",
    "openai_model": "SR_OPENAI_MODEL",
    "embed_model": "SR_EMBED_MODEL",
}

_lock = threading.Lock()


def book_path(stored: str) -> Path:
    """Book files are stored by name inside BOOKS_DIR, so a data folder can be moved or imported."""
    p = Path(stored)
    return p if p.is_absolute() and p.exists() else BOOKS_DIR / p.name


def load_settings() -> dict:
    s = dict(DEFAULTS)
    if SETTINGS_PATH.exists():
        try:
            s.update(json.loads(SETTINGS_PATH.read_text(encoding="utf-8")))
        except (OSError, json.JSONDecodeError):
            pass
    for key, env in ENV_MAP.items():
        if os.environ.get(env):
            s[key] = os.environ[env]
    if not s.get("openai_api_key") and os.environ.get("GROQ_API_KEY"):
        s["openai_api_key"] = os.environ["GROQ_API_KEY"]
    for k in ("ollama_num_ctx", "page_chars", "chunk_chars"):
        s[k] = int(s[k])
    return s


def save_settings(updates: dict) -> dict:
    with _lock:
        current = {}
        if SETTINGS_PATH.exists():
            try:
                current = json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                current = {}
        for k, v in updates.items():
            if k in DEFAULTS and v is not None:
                current[k] = v
        SETTINGS_PATH.write_text(json.dumps(current, indent=2), encoding="utf-8")
    return load_settings()
