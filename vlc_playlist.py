"""Play a playlist of media items in fullscreen using VLC."""
import sys
import json
import ctypes
import tkinter as tk
import vlc
from urllib.parse import urlparse, urlunparse

DEFAULT_IMAGE_DURATION = 5


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


def play_playlist(items: list) -> None:
    """Play media items in a loop, honoring image durations."""

    root = tk.Tk()
    root.attributes("-fullscreen", True)
    root.configure(background="black")
    frame = tk.Frame(root, background="black")
    frame.pack(fill=tk.BOTH, expand=True)

    instance = vlc.Instance()
    player = instance.media_player_new()
    root.update_idletasks()
    _attach_handle(player, frame.winfo_id())

    playlist = []
    for item in items:
        url = item.get("MediaUrl") or item.get("url")
        if url:
            url = fix_media_url(url)
        if not url:
            continue
        duration = None
        opts = []
        if is_image(item):
            # DurationSeconds is specified in seconds
            duration = float(item.get("DurationSeconds") or DEFAULT_IMAGE_DURATION)
            opts.append(f":image-duration={duration}")
        media = instance.media_new(url, *opts)
        playlist.append({"media": media, "duration": duration})

    if not playlist:
        return

    index = 0
    timer_id = None

    def play_current() -> None:
        nonlocal timer_id
        entry = playlist[index]
        player.set_media(entry["media"])
        player.play()
        if timer_id is not None:
            root.after_cancel(timer_id)
            timer_id = None
        dur = entry["duration"]
        if dur is not None:
            # Schedule next item after ``dur`` seconds for images
            timer_id = root.after(int(dur * 1000), next_item)

    def next_item() -> None:
        nonlocal index
        index = (index + 1) % len(playlist)
        play_current()

    def on_end(event) -> None:
        # Video finished; advance to next item
        root.after(0, next_item)

    player.event_manager().event_attach(
        vlc.EventType.MediaPlayerEndReached, on_end
    )

    play_current()

    root.protocol("WM_DELETE_WINDOW", lambda: (player.stop(), root.destroy()))
    root.mainloop()


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: vlc_playlist.py playlist.json")
        sys.exit(1)
    with open(sys.argv[1], "r", encoding="utf-8") as f:
        items = json.load(f)
    if not isinstance(items, list):
        print("Invalid playlist format")
        sys.exit(1)
    play_playlist(items)
