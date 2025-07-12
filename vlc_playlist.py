"""Play a playlist of media items in fullscreen using VLC."""
import sys
import json
import ctypes
import tkinter as tk
import vlc

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


def play_playlist(items: list) -> None:
    root = tk.Tk()
    root.attributes("-fullscreen", True)
    root.configure(background="black")
    frame = tk.Frame(root, background="black")
    frame.pack(fill=tk.BOTH, expand=True)

    instance = vlc.Instance()
    mlplayer = instance.media_list_player_new()
    mplayer = mlplayer.get_media_player()
    root.update_idletasks()
    _attach_handle(mplayer, frame.winfo_id())

    mlist = instance.media_list_new()
    for item in items:
        url = item.get("MediaUrl") or item.get("url")
        if not url:
            continue
        opts = []
        if is_image(item):
            dur = int(item.get("DurationSeconds") or DEFAULT_IMAGE_DURATION)
            opts.append(f":image-duration={dur}")
        media = instance.media_new(url, *opts)
        mlist.add_media(media)

    mlplayer.set_media_list(mlist)

    def on_finished(event):
        root.after(0, root.destroy)

    em = mlplayer.event_manager()
    em.event_attach(vlc.EventType.MediaListPlayerStopped, on_finished)
    # MediaListPlayerFinished does not exist; MediaListEndReached signals the
    # end of the list
    em.event_attach(vlc.EventType.MediaListEndReached, on_finished)

    mlplayer.play()
    root.protocol("WM_DELETE_WINDOW", lambda: (mlplayer.stop(), root.destroy()))
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
