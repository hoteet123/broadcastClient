"""Launch a fullscreen VLC window embedded in Tkinter."""

import sys
import ctypes
import tkinter as tk
import vlc
import pathlib
import hashlib
from urllib.parse import urlparse
import httpx
import threading
from typing import Optional


DEFAULT_URL = "http://nas.3no.kr/test.mp4"

# Directory to store cached media files
CACHE_DIR = pathlib.Path(__file__).with_name("cache")


def cache_media(url: str, progress_cb=None) -> str:
    """Return a playable URL and cache the file in the background."""
    parsed = urlparse(url)
    if parsed.scheme in {"file", ""}:
        return url

    CACHE_DIR.mkdir(exist_ok=True)
    ext = pathlib.Path(parsed.path).suffix or ".bin"
    name = hashlib.sha1(url.encode()).hexdigest() + ext
    path = CACHE_DIR / name
    if path.exists():
        return str(path)

    def _download() -> None:
        try:
            with httpx.Client(timeout=None) as cli:
                with cli.stream("GET", url) as r:
                    r.raise_for_status()
                    total = int(r.headers.get("Content-Length") or 0)
                    downloaded = 0
                    with open(path, "wb") as f:
                        for chunk in r.iter_bytes(65536):
                            if not chunk:
                                continue
                            f.write(chunk)
                            downloaded += len(chunk)
                            if progress_cb:
                                progress_cb(downloaded, total, None)
            if progress_cb:
                progress_cb(total, total, None)
        except Exception as e:  # noqa: BLE001
            if progress_cb:
                progress_cb(0, 0, e)
            try:
                path.unlink()
            except Exception:
                pass

    threading.Thread(target=_download, daemon=True).start()
    return url


def _attach_handle(player: vlc.MediaPlayer, handle: int) -> None:
    """Attach VLC player to a window handle on the current platform."""
    if sys.platform.startswith("win"):
        player.set_hwnd(handle)
    elif sys.platform == "darwin":
        # On macOS the handle needs to be passed as a void pointer
        player.set_nsobject(ctypes.c_void_p(handle))
    else:
        # X11 (Linux, Raspbian, etc.)
        player.set_xwindow(handle)


_root: tk.Tk | None = None
_player: vlc.MediaPlayer | None = None


def run(
    url: str = DEFAULT_URL,
    *,
    x: int | None = None,
    y: int | None = None,
    width: int | None = None,
    height: int | None = None,
) -> None:
    """Play ``url`` in a fullscreen window with an embedded player.

    ``x``/``y`` specify the top left corner of the embedded player within the
    fullscreen window and ``width``/``height`` control its size.  When no
    geometry is provided the player fills the entire screen.
    """
    global _root, _player
    root = tk.Tk()
    _root = root
    root.attributes("-fullscreen", True)
    root.configure(background="black")
    frame = tk.Frame(root, background="black")
    if width and height:
        fx = int(x) if x is not None else 0
        fy = int(y) if y is not None else 0
        frame.place(x=fx, y=fy, width=int(width), height=int(height))
    else:
        frame.pack(fill=tk.BOTH, expand=True)
    progress_var = tk.StringVar()
    progress_label = tk.Label(root, textvariable=progress_var, fg="white", bg="black")
    progress_label.pack(side="bottom", fill="x")
    progress_label.pack_forget()

    instance = vlc.Instance()
    player = instance.media_player_new()
    _player = player
    def on_progress(done: int, total: int, err: Optional[Exception]) -> None:
        def _update() -> None:
            if err is not None:
                progress_label.pack_forget()
                return
            if total > 0:
                pct = int(done * 100 / total)
                progress_var.set(f"Downloading... {pct}%")
            else:
                progress_var.set(f"Downloading... {done} bytes")
            if done >= total and total > 0:
                progress_label.pack_forget()
            else:
                progress_label.pack(side="bottom", fill="x")

        root.after(0, _update)

    media_url = cache_media(url, on_progress)
    media = instance.media_new(media_url)
    player.set_media(media)

    root.update_idletasks()
    handle = frame.winfo_id()
    _attach_handle(player, handle)

    player.play()
    root.protocol("WM_DELETE_WINDOW", lambda: stop())
    root.mainloop()


def stop() -> None:
    """Stop playback and close the window if running."""
    global _root, _player
    if _player is not None:
        try:
            _player.stop()
        except Exception:
            pass
        _player = None
    if _root is not None:
        try:
            _root.after(0, _root.destroy)
        except Exception:
            pass
        _root = None


# Backwards compatibility
def play_media(url: str) -> None:
    run(url)


if __name__ == '__main__':
    url = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_URL
    if len(sys.argv) < 2:
        print(f'No URL provided. Using default: {DEFAULT_URL}')
    run(url)
