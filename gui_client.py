import asyncio
import json
import threading
import sys
import tkinter as tk
import uuid
import ast
import pathlib
import os
import subprocess

DEFAULT_URL = "http://nas.3no.kr/test.mp4"

import scheduler
import tempfile
from typing import Optional
import vlc_embed
import vlc_playlist
import display_config

# Directory where the script or executable is running
RUN_DIR = pathlib.Path(sys.argv[0]).resolve().parent

"""Simple Tk GUI client with a system tray icon.

서버 연결 후 `/broadcast-schedules` 를 호출해 방송 예약 목록을 출력한다.

Dependencies::
    pip install pystray pillow
"""

try:
    from PIL import Image, ImageDraw
    import pystray
    HAS_PYSTRAY = True
except Exception as e:  # noqa: E722  (pystray may raise non-ImportError)
    print(f"pystray not available: {e}. Running without system tray icon.")
    HAS_PYSTRAY = False

import httpx
import websockets
from websockets.exceptions import ConnectionClosed

# When packaged as a single executable on Windows, ``__file__`` points to a
# temporary extraction directory. Use ``sys.argv[0]`` to locate ``client.cfg``
# next to the executable in that case.
if getattr(sys, "frozen", False) and sys.platform == "win32":
    CFG_PATH = pathlib.Path(sys.argv[0]).with_name("client.cfg")
else:
    CFG_PATH = pathlib.Path(__file__).with_name("client.cfg")


def get_mac_address() -> str:
    """Return the MAC address as a hex string without separators."""
    mac = uuid.getnode()
    return f"{mac:012X}"


def load_config():
    if not CFG_PATH.exists():
        sample = {
            "HOST": "http://example.com:65000",
            "API_KEY": "",
            "DEVICE_ID": "PC-CLIENT",
            "MAC_ADDRESS": get_mac_address(),
        }
        CFG_PATH.write_text(json.dumps(sample, indent=2), encoding="utf-8")
        print(f"Created {CFG_PATH}. Fill in API_KEY and run again.")
        sys.exit(1)
    with CFG_PATH.open("r", encoding="utf-8") as f:
        return json.load(f)


def save_config(config: dict) -> None:
    """Write the configuration dictionary to CFG_PATH."""
    CFG_PATH.write_text(json.dumps(config, indent=2), encoding="utf-8")


cfg = load_config()
HOST = cfg["HOST"].rstrip("/")
API_KEY = cfg["API_KEY"]
DEVICE_ID = cfg["DEVICE_ID"]
if cfg.get("MAC_ADDRESS"):
    MAC_ADDRESS = str(cfg["MAC_ADDRESS"])
else:
    MAC_ADDRESS = get_mac_address()
    cfg["MAC_ADDRESS"] = MAC_ADDRESS
    save_config(cfg)


class WSClient:
    def __init__(self, update_status):
        self.update_status = update_status
        self.stop_event = threading.Event()
        self.thread = threading.Thread(target=self.run, daemon=True)
        self.scheduler_thread = None
        self.scheduler_stop_event = None
        self.schedules = []
        self.playlist_items = []
        self.device_id = DEVICE_ID
        self.device_enabled = True
        self.vlc_thread = None
        self.playmode = 0
        self.vlc_x = None
        self.vlc_y = None
        self.vlc_width = None
        self.vlc_height = None
        self.gui_images = []
        self.monitor_cfgs = {
            1: {"vlc_x": None, "vlc_y": None, "vlc_width": None, "vlc_height": None},
            2: {"vlc_x": None, "vlc_y": None, "vlc_width": None, "vlc_height": None},
        }
        self.playlist_processes = {}
        self.playlist_paths = {}
        self.gui_image_paths = {}
        self.playlist_items_by_monitor = {1: [], 2: []}

    def start(self):
        self.thread.start()

    def stop(self):
        self.stop_event.set()
        if self.scheduler_stop_event:
            self.scheduler_stop_event.set()
        if self.scheduler_thread and self.scheduler_thread.is_alive():
            self.scheduler_thread.join(timeout=1)
        self.stop_vlc()

    def start_vlc(self, url: Optional[str] = None) -> None:
        """Launch VLC to play ``url`` using a thread."""
        if self.vlc_thread and self.vlc_thread.is_alive():
            return
        if not url:
            return
        target_url = url
        kwargs = {
            "x": self.vlc_x,
            "y": self.vlc_y,
            "width": self.vlc_width,
            "height": self.vlc_height,
        }
        self.vlc_thread = threading.Thread(
            target=vlc_embed.run, args=(target_url,), kwargs=kwargs, daemon=True
        )
        self.vlc_thread.start()
        vlc_embed.set_gui_images(self.gui_images)


    def start_playlist_for_monitor(self, monitor: int, items: list, start_index: int = 0) -> None:
        """Start or update playlist playback for the given monitor using a subprocess."""
        if not items:
            self.stop_playlist_for_monitor(monitor)
            return

        data = {"items": items, "start_index": int(start_index)}
        path = self.playlist_paths.get(monitor)
        if path and self.playlist_processes.get(monitor) and self.playlist_processes[monitor].poll() is None:
            try:
                with open(path, "w", encoding="utf-8") as f:
                    json.dump(data, f)
                return
            except Exception:
                pass

        self.stop_playlist_for_monitor(monitor)

        tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".json", mode="w", encoding="utf-8", dir=RUN_DIR)
        json.dump(data, tmp)
        tmp.flush(); tmp.close()
        self.playlist_paths[monitor] = tmp.name

        args = [sys.executable, pathlib.Path(__file__).with_name("vlc_playlist.py"), tmp.name]
        cfg = self.monitor_cfgs.get(monitor, {})
        if cfg.get("vlc_x") is not None:
            args += ["--x", str(int(cfg["vlc_x"]))]
        if cfg.get("vlc_y") is not None:
            args += ["--y", str(int(cfg["vlc_y"]))]
        if cfg.get("vlc_width") is not None:
            args += ["--width", str(int(cfg["vlc_width"]))]
        if cfg.get("vlc_height") is not None:
            args += ["--height", str(int(cfg["vlc_height"]))]

        if self.gui_images:
            img_tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".json", mode="w", encoding="utf-8", dir=RUN_DIR)
            json.dump(self.gui_images, img_tmp)
            img_tmp.flush(); img_tmp.close()
            args += ["--gui-images", img_tmp.name]
            self.gui_image_paths[monitor] = img_tmp.name

        proc = subprocess.Popen(args)
        self.playlist_processes[monitor] = proc

    def stop_playlist_for_monitor(self, monitor: int) -> None:
        proc = self.playlist_processes.get(monitor)
        if proc and proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=2)
            except Exception:
                proc.kill()
        self.playlist_processes.pop(monitor, None)
        path = self.playlist_paths.pop(monitor, None)
        if path:
            try:
                os.unlink(path)
            except FileNotFoundError:
                pass
        img_path = self.gui_image_paths.pop(monitor, None)
        if img_path:
            try:
                os.unlink(img_path)
            except FileNotFoundError:
                pass


    def stop_vlc(self) -> None:
        if self.vlc_thread and self.vlc_thread.is_alive():
            vlc_embed.stop()
            self.vlc_thread.join(timeout=1)
            self.vlc_thread = None
        vlc_embed.set_gui_images([])
        vlc_playlist.set_gui_images([])

        for mon in list(self.playlist_processes.keys()):
            self.stop_playlist_for_monitor(mon)

    def run(self):
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        loop.run_until_complete(self.connect_loop())

    async def play_schedule(self, sch: dict) -> None:
        """Fetch TTS audio for a schedule and play it."""
        audio = await scheduler.tts_request(
            sch.get("TTSContent", ""),
            speed=sch.get("Speed", 1.0),
            pitch=sch.get("Pitch", 1.0),
        )
        scheduler.play_mp3(audio)

    async def play_audio_url(self, url: str, volume: Optional[int] = None) -> None:
        """Download an MP3 from ``url`` and play it."""
        try:
            async with httpx.AsyncClient(timeout=120) as cli:
                r = await cli.get(url)
                r.raise_for_status()

            tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".mp3", dir=RUN_DIR)
            tmp.write(r.content)
            tmp.flush()
            tmp.close()

            if volume is not None:
                scheduler.set_volume(volume)

            scheduler.play_mp3_file(tmp.name)
        except Exception as e:  # noqa: BLE001
            print(f"Failed to play audio from {url}: {e}")

    async def connect_loop(self):
        backoff = 1
        while not self.stop_event.is_set():
            ws_url = HOST.replace("http", "ws") + (
                f"/ws?api_key={API_KEY}&device_id={self.device_id}&mac={MAC_ADDRESS}"
            )
            try:
                self.update_status("Connecting…")
                async with websockets.connect(ws_url, ping_interval=None, max_size=1 << 20) as ws:
                    self.update_status("Connected")
                    backoff = 1
                    await self.handle_ws(ws)
            except Exception:
                self.update_status(f"Disconnected: retry in {backoff}s")
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 60)

    async def update_schedules(self, *, start_scheduler: Optional[bool] = None) -> None:
        """Fetch schedules from the server and update the scheduler list.

        ``start_scheduler`` defaults to ``True`` unless ``self.playmode`` is 2.
        """
        if start_scheduler is None:
            start_scheduler = self.playmode != 2
        schedules = await scheduler.fetch_schedules(cfg)
        if self.scheduler_thread and self.scheduler_thread.is_alive():
            if self.scheduler_stop_event:
                self.scheduler_stop_event.set()
            self.scheduler_thread.join(timeout=1)
            self.scheduler_thread = None

        self.schedules = list(schedules)

        if start_scheduler and self.schedules and self.device_enabled:
            self.scheduler_stop_event = threading.Event()
            self.scheduler_thread = threading.Thread(
                target=scheduler.run,
                args=(self.schedules, self.scheduler_stop_event),
                daemon=True,
            )
            self.scheduler_thread.start()

    async def handle_ws(self, ws):
        try:
            await ws.send(json.dumps({"hello": "world", "mac": MAC_ADDRESS}))

            # 서버 설정을 받은 뒤 스케줄을 불러온다

            while not self.stop_event.is_set():
                msg = await ws.recv()
                try:
                    data = json.loads(msg)
                except json.JSONDecodeError:
                    try:
                        data = ast.literal_eval(msg)
                    except Exception:
                        print("[WS]", msg)
                        continue
                except Exception:
                    print("[WS]", msg)
                    continue

                if isinstance(data, dict) and data.get("type") == "rename":
                    new_id = data.get("device_id")
                    if new_id:
                        self.device_id = new_id
                        cfg["DEVICE_ID"] = new_id
                        save_config(cfg)
                        self.update_status(f"Renamed to {new_id}")
                elif isinstance(data, dict) and data.get("type") == "config":
                    enabled = data.get("IsEnabled", True)
                    if isinstance(enabled, str):
                        enabled = enabled.lower() in {"1", "true", "yes"}
                    else:
                        enabled = bool(enabled)
                    playmode = int(data.get("Playmode", 0))
                    self.playmode = playmode
                    dev_id = data.get("DeviceIdentifier")
                    if dev_id:
                        self.device_id = str(dev_id)
                        cfg["DEVICE_ID"] = str(dev_id)
                        save_config(cfg)
                    mon1 = data.get("Monitor1") or {}
                    mon2 = data.get("Monitor2") or {}

                    # Backwards compatibility
                    res = data.get("Resolution") or data.get("resolution")
                    orient = data.get("Orientation")
                    if res or orient is not None:
                        display_config.set_display_config(res, orient, monitor=1)
                    if isinstance(mon1, dict):
                        r1 = mon1.get("Resolution")
                        o1 = mon1.get("Orientation")
                        if r1 or o1 is not None:
                            display_config.set_display_config(r1, o1, monitor=1)
                        for key, attr in [
                            ("VlcX", "vlc_x"),
                            ("VlcY", "vlc_y"),
                            ("VlcWidth", "vlc_width"),
                            ("VlcHeight", "vlc_height"),
                        ]:
                            if mon1.get(key) is not None:
                                self.monitor_cfgs[1][attr] = int(float(mon1.get(key)))
                    if isinstance(mon2, dict):
                        r2 = mon2.get("Resolution")
                        o2 = mon2.get("Orientation")
                        if r2 or o2 is not None:
                            display_config.set_display_config(r2, o2, monitor=2)
                        for key, attr in [
                            ("VlcX", "vlc_x"),
                            ("VlcY", "vlc_y"),
                            ("VlcWidth", "vlc_width"),
                            ("VlcHeight", "vlc_height"),
                        ]:
                            if mon2.get(key) is not None:
                                self.monitor_cfgs[2][attr] = int(float(mon2.get(key)))
                    images = data.get("GuiImages")
                    if isinstance(images, list):
                        self.gui_images = list(images)
                        vlc_embed.set_gui_images(self.gui_images)
                        vlc_playlist.set_gui_images(self.gui_images)
                    try:
                        if data.get("VlcX") is not None:
                            self.vlc_x = int(float(data.get("VlcX")))
                        if data.get("VlcY") is not None:
                            self.vlc_y = int(float(data.get("VlcY")))
                        if data.get("VlcWidth") is not None:
                            self.vlc_width = int(float(data.get("VlcWidth")))
                        if data.get("VlcHeight") is not None:
                            self.vlc_height = int(float(data.get("VlcHeight")))
                    except Exception:
                        pass
                    self.device_enabled = enabled
                    if not self.device_enabled:
                        self.update_status("사용안함")
                        if self.scheduler_stop_event:
                            self.scheduler_stop_event.set()
                        if self.scheduler_thread and self.scheduler_thread.is_alive():
                            self.scheduler_thread.join(timeout=1)
                            self.scheduler_thread = None
                        self.stop_vlc()
                    else:
                        self.update_status("사용함")
                        await self.update_schedules(start_scheduler=playmode != 2)
                        if playmode in {1, 2}:
                            url = data.get("StreamURL") or data.get("url")
                            self.start_vlc(url)
                        else:
                            self.stop_vlc()
                elif isinstance(data, dict) and data.get("type") == "test-broadcast":
                    sid = data.get("schedule_id")
                    sch = next((s for s in self.schedules if s.get("ScheduleID") == sid), None)
                    if sch:
                        asyncio.create_task(self.play_schedule(sch))
                elif isinstance(data, dict) and data.get("type") == "custom-broadcast":
                    url = data.get("audio_url")
                    volume = data.get("volume")
                    if url:
                        asyncio.create_task(self.play_audio_url(url, volume))
                elif isinstance(data, dict) and data.get("type") == "play-media":
                    mid = data.get("media_id")
                    if mid is not None:
                        mid_str = str(mid)
                        for mon, items in self.playlist_items_by_monitor.items():
                            try:
                                idx = next(
                                    i for i, it in enumerate(items)
                                    if str(it.get("MediaID") or it.get("media_id") or it.get("id")) == mid_str
                                )
                                self.start_playlist_for_monitor(mon, items, start_index=idx)
                                break
                            except StopIteration:
                                continue
                elif isinstance(data, dict) and data.get("type") == "playlist":
                    items = data.get("items")
                    if isinstance(items, list):
                        new_items = list(items)
                        self.playlist_items = new_items
                        by_mon = {1: [], 2: []}
                        for it in new_items:
                            mon = int(it.get("Monitor", 1))
                            if mon == 2:
                                by_mon[2].append(it)
                            else:
                                by_mon[1].append(it)
                        self.playlist_items_by_monitor = by_mon
                        self.start_playlist_for_monitor(1, by_mon[1])
                        self.start_playlist_for_monitor(2, by_mon[2])
                elif isinstance(data, dict) and data.get("type") == "refresh-schedules":
                    await self.update_schedules(start_scheduler=self.playmode != 2)
                else:
                    print("[WS]", data)
        except ConnectionClosed:
            pass


def main():
    root = tk.Tk()
    root.title("WS Client")
    status_var = tk.StringVar(value="Starting")
    tk.Label(root, textvariable=status_var, width=40).pack(padx=20, pady=20)

    def create_image():
        image = Image.new("RGB", (64, 64), "white")
        d = ImageDraw.Draw(image)
        d.rectangle((16, 16, 48, 48), fill="black")
        return image

    def show_window():
        root.after(0, root.deiconify)

    def hide_window():
        root.after(0, root.withdraw)

    def toggle_window(icon, item):
        if root.state() == "withdrawn":
            show_window()
        else:
            hide_window()

    icon = None

    def on_close():
        client.stop()
        if icon:
            icon.stop()
        root.destroy()

    if HAS_PYSTRAY:
        tray_menu = pystray.Menu(
            pystray.MenuItem(
                lambda text: "Hide" if root.state() != "withdrawn" else "Show",
                toggle_window,
            ),
            pystray.MenuItem("Quit", lambda icon, item: root.after(0, on_close)),
        )

        icon = pystray.Icon("ws_client", create_image(), "WS Client", menu=tray_menu)

    def update_status(text):
        root.after(0, status_var.set, text)

    client = WSClient(update_status)
    client.start()

    if icon:
        threading.Thread(target=icon.run, daemon=True).start()

    root.protocol("WM_DELETE_WINDOW", on_close)
    root.mainloop()


if __name__ == "__main__":
    main()
