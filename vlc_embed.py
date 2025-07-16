"""Launch a fullscreen VLC window embedded in Tkinter."""

import sys
import ctypes
import tkinter as tk
import vlc
import pathlib
import hashlib
from urllib.parse import urlparse, urlunparse
import httpx
import io
import time
from typing import Optional, List, Dict
from PIL import Image, ImageTk, ImageSequence


DEFAULT_URL = "http://nas.3no.kr/test.mp4"

# Directory to store cached media files next to the running executable/script
RUN_DIR = pathlib.Path(sys.argv[0]).resolve().parent
CACHE_DIR = RUN_DIR / "cache"


def cache_media(url: str, progress_cb=None) -> str:
    """Download ``url`` to the cache synchronously and return the local path."""
    parsed = urlparse(url)
    if parsed.scheme in {"file", ""}:
        return url

    CACHE_DIR.mkdir(exist_ok=True)
    ext = pathlib.Path(parsed.path).suffix or ".bin"
    name = hashlib.sha1(url.encode()).hexdigest() + ext
    path = CACHE_DIR / name
    if path.exists():
        return str(path)

    tmp_path = path.with_suffix(path.suffix + ".part")
    if tmp_path.exists():
        try:
            tmp_path.unlink()
        except Exception:
            pass

    start = time.time()
    downloaded = 0
    total = 0
    try:
        with httpx.Client(timeout=None) as cli:
            with cli.stream("GET", url) as r:
                r.raise_for_status()
                total = int(r.headers.get("Content-Length") or 0)
                with open(tmp_path, "wb") as f:
                    for chunk in r.iter_bytes(65536):
                        if not chunk:
                            continue
                        f.write(chunk)
                        downloaded += len(chunk)
                        if progress_cb:
                            elapsed = time.time() - start
                            speed = downloaded / elapsed if elapsed else 0
                            progress_cb(downloaded, total, speed, elapsed, None)
        tmp_path.rename(path)
        if progress_cb:
            elapsed = time.time() - start
            speed = downloaded / elapsed if elapsed else 0
            progress_cb(downloaded, total, speed, elapsed, None)
    except Exception as e:  # noqa: BLE001
        if progress_cb:
            progress_cb(downloaded, total, 0, time.time() - start, e)
        try:
            tmp_path.unlink()
        except Exception:
            pass
        raise

    return str(path)


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
_gui_windows: List[tk.Toplevel] = []


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
    for win in _gui_windows:
        try:
            win.destroy()
        except Exception:
            pass
    _gui_windows.clear()


def _apply_gui_images() -> None:
    if _root is None or not _root.winfo_exists():
        return
    _clear_gui_images()
    base_x = _root.winfo_rootx()
    base_y = _root.winfo_rooty()
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
        try:
            x = int(float(info.get("X", 0)))
            y = int(float(info.get("Y", 0)))
        except Exception:
            x = y = 0
        width = int(float(w)) if w else frames[0].width()
        height = int(float(h)) if h else frames[0].height()

        top = tk.Toplevel(_root)
        top.overrideredirect(True)
        top.attributes("-topmost", True)
        trans = "#010203"
        try:
            top.attributes("-transparentcolor", trans)
        except Exception:
            pass
        top.configure(bg=trans)
        top.geometry(f"{width}x{height}+{base_x + x}+{base_y + y}")

        label = tk.Label(top, image=frames[0], bd=0, highlightthickness=0, bg=trans)
        label.pack(fill=tk.BOTH, expand=True)

        entry = {"label": label, "frames": frames, "delays": delays}
        _gui_labels.append(entry)
        _gui_windows.append(top)

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
    progress_label.place_forget()

    instance = vlc.Instance()
    player = instance.media_player_new()
    _player = player
    def on_progress(done: int, total: int, speed: float, elapsed: float, err: Optional[Exception]) -> None:
        if err is not None:
            progress_label.place_forget()
            root.update_idletasks()
            return
        if total > 0:
            pct = int(done * 100 / total)
            remain = 100 - pct
            unit = "KB/s" if speed < 1024 * 1024 else "MB/s"
            sp = speed / 1024 if unit == "KB/s" else speed / 1024 / 1024
            progress_var.set(f"다운로드중 {sp:.1f} {unit} {elapsed:.1f}s 남은 {remain}%")
        else:
            unit = "KB/s" if speed < 1024 * 1024 else "MB/s"
            sp = speed / 1024 if unit == "KB/s" else speed / 1024 / 1024
            progress_var.set(f"다운로드중 {sp:.1f} {unit} {elapsed:.1f}s")
        if done >= total and total > 0:
            progress_label.place_forget()
        else:
            progress_label.place(relx=0.5, rely=0.5, anchor="center")
        root.update_idletasks()

    try:
        media_url = cache_media(url, on_progress)
    except Exception as e:  # noqa: BLE001
        progress_var.set(f"Download failed: {e}")
        progress_label.place(relx=0.5, rely=0.5, anchor="center")
        root.update_idletasks()
        return

    media = instance.media_new(media_url)
    player.set_media(media)

    root.update_idletasks()
    handle = frame.winfo_id()
    _attach_handle(player, handle)

    player.play()
    _apply_gui_images()
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
