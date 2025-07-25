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
import subprocess
import tempfile
import os
import shutil
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


_roots: Dict[int, tk.Tk] = {}
_players: Dict[int, vlc.MediaPlayer] = {}
_gui_images: Dict[int, List[Dict[str, any]]] = {}
_gui_entries: Dict[int, List[Dict[str, any]]] = {}


def fix_media_url(url: str) -> str:
    """Convert old NAS URLs to the new format."""
    parsed = urlparse(url)
    if parsed.netloc == "nas.3no.kr:9006" and parsed.path.startswith("/web/"):
        new_path = parsed.path[len("/web") :]
        parsed = parsed._replace(netloc="nas.3no.kr", path=new_path)
        return urlunparse(parsed)
    return url


def _launch_chrome(
    url: str,
    *,
    x: Optional[int] = None,
    y: Optional[int] = None,
    width: Optional[int] = None,
    height: Optional[int] = None,
) -> tuple[Optional[subprocess.Popen], Optional[str]]:
    """Launch Chrome/Chromium to display ``url`` and return the process and html path."""
    exe = (
        shutil.which("google-chrome")
        or shutil.which("chromium-browser")
        or shutil.which("chromium")
        or shutil.which("chrome")
    )
    if not exe:
        return None, None
    html = (
        "<html><head>"
        "<style>html,body{margin:0;padding:0;overflow:hidden;}"
        "::-webkit-scrollbar{display:none;}</style>"
        "</head><body>"
        f"<iframe src=\"{url}\" frameborder=0 "
        "style=\"width:100%;height:100%;\"></iframe>"
        "</body></html>"
    )
    fd, path = tempfile.mkstemp(suffix=".html")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(html)
    cmd = [exe, f"--app=file://{path}", "--hide-scrollbars"]
    if width and height:
        cmd.append(f"--window-size={int(width)},{int(height)}")
    if x is not None and y is not None:
        cmd.append(f"--window-position={int(x)},{int(y)}")
    proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return proc, path


def _load_image_frames(
    url: str,
    width: Optional[int],
    height: Optional[int],
    root: tk.Tk,
) -> tuple[List, List]:
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
        frames.append(ImageTk.PhotoImage(frame, master=root))
        delays.append(int(frame.info.get("duration", 100)))
    if not frames:
        if width and height:
            img = img.resize((int(width), int(height)), Image.LANCZOS)
        frames.append(ImageTk.PhotoImage(img.convert("RGBA"), master=root))
        delays.append(int(img.info.get("duration", 100)))
    return frames, delays


def _clear_gui_elements(monitor: int) -> None:
    for entry in _gui_entries.get(monitor, []):
        player = entry.get("player")
        if player is not None:
            try:
                player.stop()
            except Exception:
                pass
        label = entry.get("label")
        if label is not None:
            try:
                label.destroy()
            except Exception:
                pass
        win = entry.get("window")
        if win is not None:
            try:
                win.destroy()
            except Exception:
                pass
        proc = entry.get("process")
        if proc is not None:
            try:
                proc.terminate()
            except Exception:
                pass
        html = entry.get("html")
        if html:
            try:
                os.unlink(html)
            except Exception:
                pass
    _gui_entries[monitor] = []


def _apply_gui_images(monitor: int) -> None:
    root = _roots.get(monitor)
    if root is None or not root.winfo_exists():
        return
    _clear_gui_elements(monitor)
    base_x = root.winfo_rootx()
    base_y = root.winfo_rooty()
    # Create windows from lowest to highest order so ``GuiOrder`` 0 is on top
    images = sorted(
        _gui_images.get(monitor, []),
        key=lambda i: int(i.get("GuiOrder", 0)),
    )
    for info in images:
        url = str(
            info.get("ImageUrl")
            or info.get("VideoUrl")
            or info.get("Url")
            or info.get("url")
            or ""
        )
        if url:
            url = fix_media_url(url)
        if not url:
            continue
        kind = str(info.get("GuiKind") or info.get("kind") or "image").lower()
        w = info.get("Width")
        h = info.get("Height")
        try:
            x = int(float(info.get("X", 0)))
            y = int(float(info.get("Y", 0)))
        except Exception:
            x = y = 0
        width = int(float(w)) if w else None
        height = int(float(h)) if h else None

        top = tk.Toplevel(root)
        top.overrideredirect(True)
        top.attributes("-topmost", True)
        trans = "#010203"
        try:
            top.attributes("-transparentcolor", trans)
        except Exception:
            pass
        top.configure(bg=trans)
        if width and height:
            top.geometry(f"{width}x{height}+{base_x + x}+{base_y + y}")
        else:
            top.geometry(f"+{base_x + x}+{base_y + y}")

        entry = {"window": top, "kind": kind}

        if kind == "image":
            try:
                frames, delays = _load_image_frames(
                    url,
                    width,
                    height,
                    root,
                )
            except Exception as e:  # noqa: BLE001
                print(f"Failed to load GUI image {url}: {e}")
                top.destroy()
                continue
            if not frames:
                top.destroy()
                continue
            label = tk.Label(top, image=frames[0], bd=0, highlightthickness=0, bg=trans)
            label.pack(fill=tk.BOTH, expand=True)
            entry.update({"label": label, "frames": frames, "delays": delays})
            if len(frames) > 1:
                def animate(idx: int = 0, lbl: tk.Label = label, frs=frames, durs=delays):
                    if not lbl.winfo_exists():
                        return
                    lbl.configure(image=frs[idx])
                    lbl.after(durs[idx], animate, (idx + 1) % len(frs), lbl, frs, durs)

                label.after(delays[0], animate, 1, label, frames, delays)
        elif kind == "video":
            frame = tk.Frame(top, background="black")
            if width and height:
                frame.place(x=0, y=0, width=int(width), height=int(height))
            else:
                frame.pack(fill=tk.BOTH, expand=True)
            instance = vlc.Instance("--no-xlib")
            player = instance.media_player_new()
            _attach_handle(player, frame.winfo_id())
            media = instance.media_new(url)
            player.set_media(media)
            player.play()
            entry.update({"player": player})
        elif kind == "url":
            proc, html = _launch_chrome(url, x=x + base_x, y=y + base_y, width=width, height=height)
            if proc is None:
                try:
                    from tkinterweb import HtmlFrame
                    web = HtmlFrame(top)
                    web.load_website(url)
                    web.pack(fill=tk.BOTH, expand=True)
                    entry.update({"web": web})
                except Exception:
                    import webbrowser
                    webbrowser.open(url)
                    top.destroy()
                    continue
            else:
                top.destroy()
                entry.update({"process": proc, "html": html, "window": None})
        else:
            top.destroy()
            continue

        _gui_entries.setdefault(monitor, []).append(entry)


def set_gui_images(images: List[Dict[str, any]], monitor: int = 1) -> None:
    """Update overlay elements (images, videos, web views) for ``monitor``."""
    _gui_images[monitor] = list(images) if images else []
    root = _roots.get(monitor)
    if root is not None:
        root.after(0, _apply_gui_images, monitor)


def run(
    url: str = DEFAULT_URL,
    *,
    x: Optional[int] = None,
    y: Optional[int] = None,
    width: Optional[int] = None,
    height: Optional[int] = None,
    monitor: int = 1,
) -> None:
    """Play ``url`` in a fullscreen window with an embedded player.

    ``x``/``y`` specify the top left corner of the embedded player within the
    fullscreen window and ``width``/``height`` control its size.  When no
    geometry is provided the player fills the entire screen.
    """
    global _roots, _players
    root = tk.Tk()
    _roots[monitor] = root
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

    # Disable direct Xlib usage to avoid threading issues on some platforms
    instance = vlc.Instance("--no-xlib")
    player = instance.media_player_new()
    _players[monitor] = player
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
    _apply_gui_images(monitor)
    root.protocol("WM_DELETE_WINDOW", lambda: stop(monitor))
    root.mainloop()


def stop(monitor: int = 1) -> None:
    """Stop playback and close the window for ``monitor`` if running."""
    player = _players.get(monitor)
    if player is not None:
        try:
            player.stop()
        except Exception:
            pass
        _players.pop(monitor, None)
    root = _roots.get(monitor)
    if root is not None:
        try:
            root.after(0, root.destroy)
        except Exception:
            pass
        _roots.pop(monitor, None)
    _clear_gui_elements(monitor)
    _gui_images.pop(monitor, None)


# Backwards compatibility
def play_media(url: str, monitor: int = 1) -> None:
    run(url, monitor=monitor)


if __name__ == '__main__':
    url = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_URL
    if len(sys.argv) < 2:
        print(f'No URL provided. Using default: {DEFAULT_URL}')
    run(url)
