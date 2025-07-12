"""Play a playlist of media items in fullscreen using VLC."""
import sys
import json
import ctypes
import tkinter as tk
import vlc
from urllib.parse import urlparse, urlunparse
import pathlib
import hashlib
import httpx

DEFAULT_IMAGE_DURATION = 5

# Directory used to store cached media files
CACHE_DIR = pathlib.Path(__file__).with_name("cache")


def cache_media(url: str) -> str:
    """Return a local path to ``url``, downloading it if needed."""
    parsed = urlparse(url)
    if parsed.scheme in {"file", ""}:
        return url

    CACHE_DIR.mkdir(exist_ok=True)
    ext = pathlib.Path(parsed.path).suffix or ".bin"
    name = hashlib.sha1(url.encode()).hexdigest() + ext
    path = CACHE_DIR / name
    if path.exists():
        return str(path)

    try:
        with httpx.Client(timeout=60.0) as cli:
            r = cli.get(url)
            r.raise_for_status()
            path.write_bytes(r.content)
        return str(path)
    except Exception as e:  # noqa: BLE001
        print(f"Failed to cache {url}: {e}")
        return url


def _attach_handle(player: vlc.MediaPlayer, handle: int) -> None:
    if sys.platform.startswith("win"):
        player.set_hwnd(handle)
    elif sys.platform == "darwin":
        player.set_nsobject(ctypes.c_void_p(handle))
    else:
        player.set_xwindow(handle)


def is_image(item: dict) -> bool:
    kind = str(item.get("MediaKind", "")).lower()
    if "image" in kind:
        return True
    url = str(item.get("MediaUrl", "")).lower()
    return url.endswith((".jpg", ".jpeg", ".png", ".gif", ".bmp"))


def fix_media_url(url: str) -> str:
    """Convert old NAS URLs to the new format."""
    parsed = urlparse(url)
    if parsed.netloc == "nas.3no.kr:9006" and parsed.path.startswith("/web/"):
        new_path = parsed.path[len("/web") :]
        parsed = parsed._replace(netloc="nas.3no.kr", path=new_path)
        return urlunparse(parsed)
    return url


def play_playlist(items: list, *, start_index: int = 0) -> None:
    root = tk.Tk()
    root.attributes("-fullscreen", True)
    root.configure(background="black")
    frame = tk.Frame(root, background="black")
    frame.pack(fill=tk.BOTH, expand=True)

    instance = vlc.Instance()
    player = instance.media_player_new()
    root.update_idletasks()
    _attach_handle(player, frame.winfo_id())

    idx = max(0, int(start_index))

    if not items:
        root.destroy()
        return

    def play_next() -> None:
        nonlocal idx
        if idx >= len(items):
            idx = 0
        item = items[idx]
        idx += 1

        url = item.get("MediaUrl") or item.get("url")
        if url:
            url = fix_media_url(url)
        if not url:
            root.after(0, play_next)
            return

        media_url = cache_media(url)
        media = instance.media_new(media_url)
        player.set_media(media)

        volume = item.get("Volume")
        if volume is None:
            volume = item.get("volume")
        if volume is not None:
            try:
                vol = int(float(volume))
            except Exception:
                vol = None
        else:
            vol = None

        player.play()

        if vol is not None:
            try:
                player.audio_set_volume(max(0, min(100, vol)))
            except Exception:
                pass

        if is_image(item):
            dur = int(item.get("DurationSeconds") or DEFAULT_IMAGE_DURATION)
            root.after(dur * 1000, play_next)
        else:
            def on_end(event):
                player.event_manager().event_detach(vlc.EventType.MediaPlayerEndReached)
                root.after(0, play_next)

            player.event_manager().event_attach(
                vlc.EventType.MediaPlayerEndReached, on_end
            )

    play_next()

    root.protocol("WM_DELETE_WINDOW", lambda: (player.stop(), root.destroy()))
    root.mainloop()


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: vlc_playlist.py playlist.json")
        sys.exit(1)
    with open(sys.argv[1], "r", encoding="utf-8") as f:
        data = json.load(f)
    if isinstance(data, dict):
        items = data.get("items", [])
        start_index = int(data.get("start_index", 0))
    else:
        items = data
        start_index = 0
    if not isinstance(items, list):
        print("Invalid playlist format")
        sys.exit(1)
    play_playlist(items, start_index=start_index)
