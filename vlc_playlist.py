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


_roots: Dict[int, tk.Tk] = {}
_players: Dict[int, vlc.MediaPlayer] = {}
_after_ids: Dict[int, str] = {}
_check_ids: Dict[int, str] = {}
_playlist_paths: Dict[int, str] = {}
_items_map: Dict[int, list] = {}
_idx_map: Dict[int, int] = {}
_last_mtimes: Dict[int, float] = {}
_gui_images: Dict[int, List[Dict[str, any]]] = {}
_gui_entries: Dict[int, List[Dict[str, any]]] = {}


def _load_image_frames(
    url: str,
    width: Optional[int],
    height: Optional[int],
    root: tk.Tk,
) -> tuple[List, List]:
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
                if win.winfo_exists():
                    win.destroy()
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
    # Create windows from highest to lowest order so overlays with smaller
    # ``GuiOrder`` values are instantiated last and therefore remain on top
    images = sorted(
        _gui_images.get(monitor, []),
        key=lambda i: int(i.get("GuiOrder", 0)),
        reverse=True,
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
            try:
                from tkinterweb import HtmlFrame
                web = HtmlFrame(
                    top,
                    horizontal_scrollbar=False,
                    vertical_scrollbar=False,
                )
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
            continue

        if top.winfo_exists():
            top.lift()
        _gui_entries.setdefault(monitor, []).append(entry)


def set_gui_images(images: List[Dict[str, any]], monitor: int = 1) -> None:
    """Update overlay elements (images, videos, web views) for ``monitor``."""
    _gui_images[monitor] = list(images) if images else []
    root = _roots.get(monitor)
    if root is not None:
        root.after(0, _apply_gui_images, monitor)


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
    monitor: int = 1,
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

    global _roots, _players, _after_ids, _check_ids, _playlist_paths, _items_map, _idx_map, _last_mtimes

    _playlist_paths[monitor] = path

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
    root.update_idletasks()
    _attach_handle(player, frame.winfo_id())
    _apply_gui_images(monitor)

    items, idx = load()
    _items_map[monitor] = items
    idx = max(0, int(idx))
    _idx_map[monitor] = idx
    last_mtime = os.path.getmtime(path) if os.path.exists(path) else 0.0
    _last_mtimes[monitor] = last_mtime
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
        _idx_map[monitor] = idx

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
            _after_ids[monitor] = after_id
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
            _check_ids[monitor] = root.after(1000, check_update)
            return
        if mtime != last_mtime:
            last_mtime = mtime
            new_items, new_idx = load()
            if new_items:
                items = new_items
                _items_map[monitor] = items
                idx = max(0, int(new_idx))
                _idx_map[monitor] = idx
                player.stop()
                play_next()
        _check_ids[monitor] = root.after(1000, check_update)

    play_next()
    _check_ids[monitor] = root.after(1000, check_update)

    root.protocol("WM_DELETE_WINDOW", lambda: stop(monitor))
    root.mainloop()


def stop(monitor: int = 1) -> None:
    """Stop playback and close the window for ``monitor`` if running."""
    root = _roots.get(monitor)
    if root is None:
        return
    after_id = _after_ids.get(monitor)
    if after_id is not None:
        try:
            root.after_cancel(after_id)
        except Exception:
            pass
        _after_ids.pop(monitor, None)
    check_id = _check_ids.get(monitor)
    if check_id is not None:
        try:
            root.after_cancel(check_id)
        except Exception:
            pass
        _check_ids.pop(monitor, None)
    player = _players.get(monitor)
    if player is not None:
        try:
            player.stop()
        except Exception:
            pass
        _players.pop(monitor, None)
    try:
        root.after(0, root.destroy)
    except Exception:
        pass
    _roots.pop(monitor, None)
    _clear_gui_elements(monitor)
    _gui_images.pop(monitor, None)


def play_playlist(path: str, monitor: int = 1) -> None:
    """Backward compatible wrapper for ``run``."""
    run(path, monitor=monitor)


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: vlc_playlist.py playlist.json")
        sys.exit(1)

    run(sys.argv[1])
