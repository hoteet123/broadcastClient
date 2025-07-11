"""Launch a fullscreen VLC window embedded in Tkinter."""

import sys
import ctypes
import tkinter as tk
import vlc


DEFAULT_URL = "http://nas.3no.kr/test.mp4"


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


def play_media(url: str) -> None:
    root = tk.Tk()
    root.attributes("-fullscreen", True)
    root.configure(background="black")
    frame = tk.Frame(root, background="black")
    frame.pack(fill=tk.BOTH, expand=True)

    instance = vlc.Instance()
    player = instance.media_player_new()
    media = instance.media_new(url)
    player.set_media(media)

    root.update_idletasks()
    handle = frame.winfo_id()
    _attach_handle(player, handle)

    player.play()
    root.protocol("WM_DELETE_WINDOW", lambda: (player.stop(), root.destroy()))
    root.mainloop()


if __name__ == '__main__':
    url = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_URL
    if len(sys.argv) < 2:
        print(f'No URL provided. Using default: {DEFAULT_URL}')
    play_media(url)
