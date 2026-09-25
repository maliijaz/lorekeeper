"""First-run helpers: find/start Ollama and download the selected model with progress."""
import json
import os
import shutil
import subprocess
import threading
import time
from pathlib import Path

import httpx

from .config import load_settings

_pull = {"model": None, "status": "", "completed": 0, "total": 0, "error": None, "active": False}
_lock = threading.Lock()


def ollama_exe() -> str | None:
    found = shutil.which("ollama")
    if found:
        return found
    for base in (os.environ.get("LOCALAPPDATA", ""), os.environ.get("ProgramFiles", "")):
        p = Path(base) / "Programs" / "Ollama" / "ollama.exe" if base else None
        if p and p.exists():
            return str(p)
    p = Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "Ollama" / "ollama.exe"
    return str(p) if p.exists() else None


def ollama_running(url: str | None = None) -> bool:
    url = url or load_settings()["ollama_url"]
    try:
        return httpx.get(f"{url}/api/version", timeout=2).status_code == 200
    except httpx.HTTPError:
        return False


def start_ollama(wait: float = 20.0) -> bool:
    """Start `ollama serve` in the background (no console window) if it isn't already running."""
    s = load_settings()
    if ollama_running(s["ollama_url"]):
        return True
    exe = ollama_exe()
    if not exe:
        return False
    flags = 0
    if os.name == "nt":
        flags = subprocess.CREATE_NO_WINDOW | subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
    subprocess.Popen([exe, "serve"], creationflags=flags, stdout=subprocess.DEVNULL,
                     stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL, close_fds=True)
    deadline = time.time() + wait
    while time.time() < deadline:
        if ollama_running(s["ollama_url"]):
            return True
        time.sleep(0.5)
    return False


def model_present(model: str, url: str) -> bool:
    try:
        names = [m["name"] for m in httpx.get(f"{url}/api/tags", timeout=5).json().get("models", [])]
    except (httpx.HTTPError, ValueError):
        return False
    return model in names or f"{model}:latest" in names


def status() -> dict:
    s = load_settings()
    out = {"provider": s["provider"], "model": s["ollama_model"], "ollama_installed": bool(ollama_exe()),
           "ollama_running": False, "model_present": False, "pull": dict(_pull)}
    if s["provider"] != "ollama":
        out["ready"] = bool(s.get("openai_api_key")) or "localhost" in s["openai_base_url"]
        return out
    out["ollama_running"] = ollama_running(s["ollama_url"])
    if out["ollama_running"]:
        out["model_present"] = model_present(s["ollama_model"], s["ollama_url"])
    out["ready"] = out["ollama_running"] and out["model_present"]
    return out


def pull_model(model: str | None = None) -> dict:
    s = load_settings()
    model = model or s["ollama_model"]
    with _lock:
        if _pull["active"]:
            return dict(_pull)
        _pull.update(model=model, status="starting", completed=0, total=0, error=None, active=True)
    threading.Thread(target=_pull_worker, args=(model, s["ollama_url"]), daemon=True).start()
    return dict(_pull)


def _pull_worker(model: str, url: str):
    try:
        if not start_ollama():
            raise RuntimeError("Ollama is not installed or could not be started.")
        with httpx.stream("POST", f"{url}/api/pull", json={"model": model, "stream": True},
                          timeout=httpx.Timeout(None, connect=10)) as r:
            for line in r.iter_lines():
                if not line:
                    continue
                ev = json.loads(line)
                if ev.get("error"):
                    raise RuntimeError(ev["error"])
                _pull["status"] = ev.get("status", "")
                if ev.get("total"):
                    _pull["total"] = ev["total"]
                    _pull["completed"] = ev.get("completed", 0)
        _pull["status"] = "success"
    except Exception as e:  # noqa: BLE001 - shown in the UI
        _pull["error"] = str(e)
        _pull["status"] = "error"
    finally:
        _pull["active"] = False
