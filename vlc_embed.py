"""Launch a fullscreen VLC window embedded in Tkinter."""

import sys
import ctypes
import tkinter as tk
import vlc


DEFAULT_URL = "http://nas.3no.kr/test.mp4"



def cache_media(url: str) -> str:
    """Return the given URL without caching it locally."""
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


def run(url: str = DEFAULT_URL) -> None:
    """Play ``url`` in an embedded fullscreen window."""
    global _root, _player
    root = tk.Tk()
    _root = root
    root.attributes("-fullscreen", True)
    root.configure(background="black")
    frame = tk.Frame(root, background="black")
    frame.pack(fill=tk.BOTH, expand=True)

    instance = vlc.Instance()
    player = instance.media_player_new()
    _player = player
    media_url = cache_media(url)
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
