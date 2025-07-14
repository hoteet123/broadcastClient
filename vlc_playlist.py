"""Play a playlist of media items in fullscreen using VLC.

This script keeps the playback window alive even when the playlist file is
updated.  When the JSON playlist file given on the command line changes, the
file is reloaded and playback continues without closing the window.  The file
format is the same as previously used by ``WSClient.start_vlc_playlist``:

``{"items": [...], "start_index": 0}``
"""

import sys
import json
import os
import ctypes
import tkinter as tk
import vlc
from urllib.parse import urlparse, urlunparse

DEFAULT_IMAGE_DURATION = 5



def cache_media(url: str) -> str:
    """Return the given URL without caching it locally."""
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


def play_playlist(path: str) -> None:
    """Play playlist defined in ``path`` and reload when it changes."""

    def load() -> tuple[list, int]:
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                items = data.get("items", [])
                start_idx = int(data.get("start_index", 0))
            else:
                items = data
                start_idx = 0
            if not isinstance(items, list):
                items = []
            return items, start_idx
        except Exception as e:  # noqa: BLE001
            print(f"Failed to load playlist: {e}")
            return [], 0

    root = tk.Tk()
    root.attributes("-fullscreen", True)
    root.configure(background="black")
    frame = tk.Frame(root, background="black")
    frame.pack(fill=tk.BOTH, expand=True)

    instance = vlc.Instance()
    player = instance.media_player_new()
    root.update_idletasks()
    _attach_handle(player, frame.winfo_id())

    items, idx = load()
    idx = max(0, int(idx))
    last_mtime = os.path.getmtime(path) if os.path.exists(path) else 0.0
    after_id = None

    def play_next() -> None:
        nonlocal idx, after_id
        if after_id is not None:
            root.after_cancel(after_id)
            after_id = None
        if not items:
            return
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
            after_id = root.after(dur * 1000, play_next)
        else:
            def on_end(event):
                player.event_manager().event_detach(vlc.EventType.MediaPlayerEndReached)
                root.after(0, play_next)

            player.event_manager().event_attach(
                vlc.EventType.MediaPlayerEndReached, on_end
            )

    def check_update() -> None:
        nonlocal items, idx, last_mtime
        try:
            mtime = os.path.getmtime(path)
        except FileNotFoundError:
            root.after(1000, check_update)
            return
        if mtime != last_mtime:
            last_mtime = mtime
            new_items, new_idx = load()
            if new_items:
                items = new_items
                idx = max(0, int(new_idx))
                player.stop()
                play_next()
        root.after(1000, check_update)

    play_next()
    root.after(1000, check_update)

    root.protocol("WM_DELETE_WINDOW", lambda: (player.stop(), root.destroy()))
    root.mainloop()


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: vlc_playlist.py playlist.json")
        sys.exit(1)

    play_playlist(sys.argv[1])
