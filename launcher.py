"""Lorekeeper desktop app: starts the local server (and Ollama) and opens a native window.

Run with pythonw.exe for a console-free app (that's what the installed shortcuts do).
Closing the window quits the app; book analysis resumes where it left off on next launch.
"""
import os
import socket
import sys
import threading
import time
import webbrowser
from pathlib import Path

ROOT = Path(__file__).resolve().parent
os.chdir(ROOT)
sys.path.insert(0, str(ROOT))

from app.config import DATA_DIR  # noqa: E402

LOG_DIR = DATA_DIR / "logs"
LOG_DIR.mkdir(parents=True, exist_ok=True)
if sys.stdout is None or "pythonw" in Path(sys.executable).name.lower():
    # pythonw has no console; send all output to a log file instead.
    log = open(LOG_DIR / "app.log", "a", encoding="utf-8", buffering=1)
    sys.stdout = sys.stderr = log

import httpx  # noqa: E402

TITLE = "Lorekeeper"
PREFERRED_PORT = int(os.environ.get("SR_PORT", "8765"))

LOADING_HTML = """<!doctype html><html><head><meta charset="utf-8"><style>
body{margin:0;height:100vh;display:grid;place-items:center;background:#fbfaf7;color:#22201c;
font:15px system-ui,sans-serif}@media(prefers-color-scheme:dark){body{background:#1d1f23;color:#e4e1da}}
.box{text-align:center}.t{font:600 30px Georgia,serif;margin:18px 0 6px}.m{opacity:.65}
.s{width:26px;height:26px;margin:22px auto 0;border:3px solid #d8cdb8;border-top-color:#b07a3f;
border-radius:50%;animation:r .8s linear infinite}@keyframes r{to{transform:rotate(360deg)}}
</style></head><body><div class="box"><div class="t">Lorekeeper</div>
<div class="m" id="m">Starting up&hellip;</div><div class="s"></div></div></body></html>"""


def ours(port: int) -> bool:
    try:
        return httpx.get(f"http://127.0.0.1:{port}/api/ping", timeout=1.5).json().get("app") == "lorekeeper"
    except Exception:
        return False


def port_free(port: int) -> bool:
    with socket.socket() as s:
        try:
            s.bind(("127.0.0.1", port))
            return True
        except OSError:
            return False


def pick_port() -> tuple[int, bool]:
    """Returns (port, already_running)."""
    if ours(PREFERRED_PORT):
        return PREFERRED_PORT, True
    if port_free(PREFERRED_PORT):
        return PREFERRED_PORT, False
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1], False


def run_server(port: int):
    import uvicorn

    config = uvicorn.Config("app.main:app", host="127.0.0.1", port=port, log_level="info",
                            use_colors=False)
    uvicorn.Server(config).run()


def start_ollama_bg():
    try:
        from app import setup
        from app.config import load_settings

        if load_settings()["provider"] == "ollama":
            setup.start_ollama(wait=0)
    except Exception as e:  # noqa: BLE001
        print("ollama start failed:", e)


def set_app_id():
    """Give the process its own taskbar identity (instead of grouping with Python)."""
    if os.name == "nt":
        try:
            import ctypes
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("Lorekeeper.App")
        except Exception:
            pass


def set_window_icon(title: str, icon: Path, tries: int = 40):
    if os.name != "nt" or not icon.exists():
        return
    import ctypes

    user32 = ctypes.windll.user32
    user32.FindWindowW.restype = ctypes.c_void_p
    user32.LoadImageW.restype = ctypes.c_void_p
    user32.SendMessageW.argtypes = [ctypes.c_void_p, ctypes.c_uint, ctypes.c_void_p, ctypes.c_void_p]
    for _ in range(tries):
        hwnd = user32.FindWindowW(None, title)
        if hwnd:
            for which, size in ((1, 256), (0, 32)):  # ICON_BIG, ICON_SMALL
                h = user32.LoadImageW(None, str(icon), 1, size, size, 0x10)  # IMAGE_ICON, LR_LOADFROMFILE
                if h:
                    user32.SendMessageW(hwnd, 0x80, which, h)  # WM_SETICON
            return
        time.sleep(0.25)


def wait_ready(port: int, timeout: float = 180) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if ours(port):
            return True
        time.sleep(0.3)
    return False


def main():
    set_app_id()
    port, running = pick_port()
    url = f"http://127.0.0.1:{port}/"
    threading.Thread(target=start_ollama_bg, daemon=True).start()
    if not running:
        threading.Thread(target=run_server, args=(port,), daemon=True, name="server").start()

    try:
        import webview
    except ImportError:
        webview = None

    if webview is None:  # fallback: default browser, keep the server alive until killed
        if wait_ready(port):
            webbrowser.open(url)
        threading.Event().wait()
        return

    window = webview.create_window(TITLE, html=LOADING_HTML, width=1440, height=920, min_size=(760, 560),
                                   background_color="#fbfaf7", text_select=True)

    icon = ROOT / "assets" / "icon.ico"

    def navigate():
        set_window_icon(TITLE, icon)
        if wait_ready(port):
            window.load_url(url)
        else:
            window.evaluate_js("document.getElementById('m').textContent="
                               f"'Could not start. See {str(LOG_DIR / 'app.log').replace(chr(92), '/')}'")

    webview.start(navigate, private_mode=False, storage_path=str(DATA_DIR / "webview"))
    os._exit(0)  # window closed: stop server and background analysis (it resumes next time)


if __name__ == "__main__":
    main()
