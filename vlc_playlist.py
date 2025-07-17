"""Play a playlist of media items in fullscreen using VLC.

The functions here mirror the previous standalone script behaviour but allow
embedding the player in the current process.  ``run()`` starts playback of a
playlist JSON file and automatically reloads the file when it changes.
``stop()`` closes the window and stops playback.  The JSON format is the same
as previously used by ``WSClient.start_vlc_playlist``:

``{"items": [...], "start_index": 0}``
"""

import sys
import json
import os
import ctypes
import tkinter as tk
import vlc
from urllib.parse import urlparse, urlunparse
import pathlib
import hashlib
import httpx
import io
import time
from typing import Optional, List, Dict
from PIL import Image, ImageTk, ImageSequence

DEFAULT_IMAGE_DURATION = 5

# Directory used to store cached media files next to the running executable/script
RUN_DIR = pathlib.Path(sys.argv[0]).resolve().parent
CACHE_DIR = RUN_DIR / "cache"


_root: Optional[tk.Tk] = None
_player: Optional[vlc.MediaPlayer] = None
_after_id: Optional[str] = None
_check_id: Optional[str] = None
_playlist_path: Optional[str] = None
_items: Optional[list] = None
_idx: int = 0
_last_mtime: float = 0.0
_gui_images: List[Dict[str, any]] = []
_gui_labels: List[Dict[str, any]] = []
_gui_windows: List[tk.Toplevel] = []


def _load_image_frames(url: str, width: Optional[int], height: Optional[int]) -> tuple[List, List]:
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
    global _gui_images
    _gui_images = list(images) if images else []
    if _root is not None:
        _root.after(0, _apply_gui_images)


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


def run(
    path: str,
    *,
    x: Optional[int] = None,
    y: Optional[int] = None,
    width: Optional[int] = None,
    height: Optional[int] = None,
) -> None:
    """Play playlist defined in ``path`` and reload when it changes.

    The player always opens a fullscreen window.  When ``width`` and ``height``
    are supplied the VLC player is embedded at ``x``, ``y`` with the given size.
    Otherwise the player fills the entire window.
    """

    def load() -> tuple[List, int]:
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

    global _root, _player, _after_id, _check_id, _playlist_path, _items, _idx, _last_mtime

    _playlist_path = path

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

    # Disable direct Xlib usage to avoid threading issues on some platforms
    instance = vlc.Instance("--no-xlib")
    player = instance.media_player_new()
    _player = player
    root.update_idletasks()
    _attach_handle(player, frame.winfo_id())
    _apply_gui_images()

    items, idx = load()
    _items = items
    idx = max(0, int(idx))
    _idx = idx
    last_mtime = os.path.getmtime(path) if os.path.exists(path) else 0.0
    _last_mtime = last_mtime
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
        _idx = idx

        url = item.get("MediaUrl") or item.get("url")
        if url:
            url = fix_media_url(url)
        if not url:
            root.after(0, play_next)
            return

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
            root.after(2000, play_next)
            return
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
            _after_id = after_id
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
            _check_id = root.after(1000, check_update)
            return
        if mtime != last_mtime:
            last_mtime = mtime
            new_items, new_idx = load()
            if new_items:
                items = new_items
                _items = items
                idx = max(0, int(new_idx))
                _idx = idx
                player.stop()
                play_next()
        _check_id = root.after(1000, check_update)

    play_next()
    _check_id = root.after(1000, check_update)

    root.protocol("WM_DELETE_WINDOW", lambda: stop())
    root.mainloop()


def stop() -> None:
    """Stop playback and close the window if running."""
    global _root, _player, _after_id, _check_id, _gui_images
    if _root is None:
        return
    if _after_id is not None:
        try:
            _root.after_cancel(_after_id)
        except Exception:
            pass
        _after_id = None
    if _check_id is not None:
        try:
            _root.after_cancel(_check_id)
        except Exception:
            pass
        _check_id = None
    if _player is not None:
        try:
            _player.stop()
        except Exception:
            pass
        _player = None
    try:
        _root.after(0, _root.destroy)
    except Exception:
        pass
    _root = None
    _clear_gui_images()
    _gui_images = []


def play_playlist(path: str) -> None:
    """Backward compatible wrapper for ``run``."""
    run(path)


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: vlc_playlist.py playlist.json")
        sys.exit(1)

    run(sys.argv[1])
