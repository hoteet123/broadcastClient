"""Launch a fullscreen VLC window embedded in Tkinter."""

import sys
import ctypes
import tkinter as tk
import vlc
import pathlib
import hashlib
from urllib.parse import urlparse, urlunparse
import httpx
import threading
from media_cache import download_media
import io
from typing import Optional, List, Dict
from PIL import Image, ImageTk, ImageSequence


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


_root: Optional[tk.Tk] = None
_player: Optional[vlc.MediaPlayer] = None
_gui_images: List[Dict[str, any]] = []
_gui_labels: List[Dict[str, any]] = []


def fix_media_url(url: str) -> str:
    """Convert old NAS URLs to the new format."""
    parsed = urlparse(url)
    if parsed.netloc == "nas.3no.kr:9006" and parsed.path.startswith("/web/"):
        new_path = parsed.path[len("/web") :]
        parsed = parsed._replace(netloc="nas.3no.kr", path=new_path)
        return urlunparse(parsed)
    return url


def _load_image_frames(url: str, width: Optional[int], height: Optional[int]) -> tuple[List, List]:
    """Return a list of PhotoImage frames and their durations."""
    try:
        if urlparse(url).scheme in {"http", "https"}:
            r = httpx.get(url, timeout=30)
            r.raise_for_status()
            data = io.BytesIO(r.content)
            img = Image.open(data)
        else:
            img = Image.open(url)
    except Exception as e:  # noqa: BLE001
        raise RuntimeError(e)

    frames = []
    delays = []
    for frame in ImageSequence.Iterator(img):
        if width and height:
            frame = frame.resize((int(width), int(height)), Image.LANCZOS)
        frame = frame.convert("RGBA")
        frames.append(ImageTk.PhotoImage(frame, master=_root))
        delays.append(int(frame.info.get("duration", 100)))
    if not frames:
        if width and height:
            img = img.resize((int(width), int(height)), Image.LANCZOS)
        frames.append(ImageTk.PhotoImage(img.convert("RGBA"), master=_root))
        delays.append(int(img.info.get("duration", 100)))
    return frames, delays


def _clear_gui_images() -> None:
    for item in _gui_labels:
        try:
            item["label"].destroy()
        except Exception:
            pass
    _gui_labels.clear()


def _apply_gui_images() -> None:
    if _root is None or not _root.winfo_exists():
        return
    _clear_gui_images()
    for info in _gui_images:
        url = str(info.get("ImageUrl") or info.get("url") or "")
        if url:
            url = fix_media_url(url)
        if not url:
            continue
        w = info.get("Width")
        h = info.get("Height")
        try:
            frames, delays = _load_image_frames(url, int(float(w)) if w else None, int(float(h)) if h else None)
        except Exception as e:  # noqa: BLE001
            print(f"Failed to load GUI image {url}: {e}")
            continue
        if not frames:
            continue
        label = tk.Label(_root, image=frames[0], bd=0, highlightthickness=0)
        try:
            x = int(float(info.get("X", 0)))
            y = int(float(info.get("Y", 0)))
        except Exception:
            x = y = 0
        opts = {"x": x, "y": y}
        if w and h:
            try:
                opts["width"] = int(float(w))
                opts["height"] = int(float(h))
            except Exception:
                pass
        label.place(**opts)
        entry = {"label": label, "frames": frames, "delays": delays}
        _gui_labels.append(entry)

        if len(frames) > 1:
            def animate(idx: int = 0, lbl: tk.Label = label, frs=frames, durs=delays):
                if not lbl.winfo_exists():
                    return
                lbl.configure(image=frs[idx])
                lbl.after(durs[idx], animate, (idx + 1) % len(frs), lbl, frs, durs)

            label.after(delays[0], animate, 1, label, frames, delays)


def set_gui_images(images: List[Dict[str, any]]) -> None:
    """Update GUI overlay images."""
    global _gui_images
    _gui_images = list(images) if images else []
    if _root is not None:
        _root.after(0, _apply_gui_images)


def run(
    url: str = DEFAULT_URL,
    *,
    x: Optional[int] = None,
    y: Optional[int] = None,
    width: Optional[int] = None,
    height: Optional[int] = None,
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
    progress_label.place(relx=0.5, rely=0.5, anchor="center")
    progress_label.lift()
    url = fix_media_url(url)
    instance = vlc.Instance()
    player = instance.media_player_new()
    _player = player

    def on_progress(done: int, total: int, speed: float, elapsed: float) -> None:
        def _update() -> None:
            pct = int(done * 100 / total) if total > 0 else 0
            text = f"다운로드중 {speed/1024:.1f} KB/s {elapsed:.1f}s {pct}%"
            progress_var.set(text)

        root.after(0, _update)

    media_path: dict = {}

    def start_playback() -> None:
        path = media_path.get("path")
        if not path:
            root.destroy()
            return
        progress_label.place_forget()
        media = instance.media_new(path)
        player.set_media(media)
        root.update_idletasks()
        handle = frame.winfo_id()
        _attach_handle(player, handle)
        player.play()
        _apply_gui_images()

    def download_thread() -> None:
        try:
            media_path["path"] = download_media(url, on_progress)
        finally:
            root.after(0, start_playback)

    threading.Thread(target=download_thread, daemon=True).start()

    root.protocol("WM_DELETE_WINDOW", lambda: stop())
    root.mainloop()


def stop() -> None:
    """Stop playback and close the window if running."""
    global _root, _player, _gui_images
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
    _clear_gui_images()
    _gui_images = []


# Backwards compatibility
def play_media(url: str) -> None:
    run(url)


if __name__ == '__main__':
    url = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_URL
    if len(sys.argv) < 2:
        print(f'No URL provided. Using default: {DEFAULT_URL}')
    run(url)
