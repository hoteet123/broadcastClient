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
import time

DEFAULT_URL = "http://nas.3no.kr/test.mp4"

import scheduler
import tempfile
from typing import Optional
from urllib.parse import urlparse
import vlc_embed
import vlc_playlist
import display_config

HOST_URL = "https://api.flexx.kr:65000"

# Directory where the script or executable is running
RUN_DIR = pathlib.Path(sys.argv[0]).resolve().parent
# When packaged by PyInstaller, data files such as the VNC binaries are
# extracted to ``sys._MEIPASS``. Use that as the base directory for bundled
# resources while falling back to ``RUN_DIR`` when running from sources.
BUNDLE_DIR = pathlib.Path(getattr(sys, "_MEIPASS", RUN_DIR))

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
            "HOST": HOST_URL,
            "API_KEY": "",
            "DEVICE_ID": "PC-CLIENT",
            "MAC_ADDRESS": get_mac_address(),
        }
        CFG_PATH.write_text(json.dumps(sample, indent=2), encoding="utf-8")
        print(f"Created {CFG_PATH}. Fill in API_KEY and run again.")
        sys.exit(1)
    with CFG_PATH.open("r", encoding="utf-8") as f:
        cfg = json.load(f)
    cfg["HOST"] = HOST_URL
    save_config(cfg)
    return cfg


def save_config(config: dict) -> None:
    """Write the configuration dictionary to CFG_PATH."""
    CFG_PATH.write_text(json.dumps(config, indent=2), encoding="utf-8")


cfg = load_config()
HOST = HOST_URL.rstrip("/")
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
        self.monitors = {
            1: {
                "vlc_thread": None,
                "playlist_thread": None,
                "playlist_path": None,
                "vlc_x": None,
                "vlc_y": None,
                "vlc_width": None,
                "vlc_height": None,
                "playlist_items": [],
            },
            2: {
                "vlc_thread": None,
                "playlist_thread": None,
                "playlist_path": None,
                "vlc_x": None,
                "vlc_y": None,
                "vlc_width": None,
                "vlc_height": None,
                "playlist_items": [],
            },
        }
        self.playmode = 0
        self.monitor_count = display_config.get_monitor_count()
        self.gui_images = []
        self.gui_images_by_monitor = {1: [], 2: []}
        self.last_update_time = 0.0
        self.vnc_enabled = False
        self.vnc_port: Optional[int] = None
        self.vnc_password: Optional[str] = None

    def start(self):
        self.thread.start()

    def stop(self):
        self.stop_event.set()
        if self.scheduler_stop_event:
            self.scheduler_stop_event.set()
        if self.scheduler_thread and self.scheduler_thread.is_alive():
            self.scheduler_thread.join(timeout=1)
        self.stop_vlc()
        self.stop_vnc_server()

    def start_vlc(self, url: Optional[str] = None, monitor: int = 1) -> None:
        """Launch VLC to play ``url`` on ``monitor`` using a thread."""
        if monitor > self.monitor_count:
            return
        mon = self.monitors.get(monitor)
        if not mon:
            return
        if mon["vlc_thread"] and mon["vlc_thread"].is_alive():
            return
        if not url:
            return
        kwargs = {
            "x": mon["vlc_x"],
            "y": mon["vlc_y"],
            "width": mon["vlc_width"],
            "height": mon["vlc_height"],
            "monitor": monitor,
        }
        mon["vlc_thread"] = threading.Thread(
            target=vlc_embed.run, args=(url,), kwargs=kwargs, daemon=True
        )
        mon["vlc_thread"].start()
        vlc_embed.set_gui_images(self.gui_images_by_monitor.get(monitor, []), monitor)

    def start_vlc_playlist(self, items: list, start_index: int = 0, monitor: int = 1) -> None:
        """Launch or update VLC playlist on ``monitor`` without closing the window."""
        if monitor > self.monitor_count:
            return
        mon = self.monitors.get(monitor)
        if not mon:
            return

        if not items:
            self.stop_vlc(monitor)
            return

        data = {"items": items, "start_index": int(start_index)}
        if mon["playlist_thread"] and mon["playlist_thread"].is_alive() and mon["playlist_path"]:
            try:
                with open(mon["playlist_path"], "w", encoding="utf-8") as f:
                    json.dump(data, f)
                return
            except Exception:
                pass

        self.stop_vlc(monitor)

        tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".json", mode="w", encoding="utf-8", dir=RUN_DIR)
        json.dump(data, tmp)
        tmp.flush()
        tmp.close()
        mon["playlist_path"] = tmp.name
        kwargs = {
            "x": mon["vlc_x"],
            "y": mon["vlc_y"],
            "width": mon["vlc_width"],
            "height": mon["vlc_height"],
            "monitor": monitor,
        }
        mon["playlist_thread"] = threading.Thread(
            target=vlc_playlist.run,
            args=(mon["playlist_path"],),
            kwargs=kwargs,
            daemon=True,
        )
        mon["playlist_thread"].start()
        vlc_playlist.set_gui_images(
            self.gui_images_by_monitor.get(monitor, []), monitor
        )


    def stop_vlc(self, monitor: Optional[int] = None) -> None:
        mons = [monitor] if monitor else list(self.monitors.keys())
        mons = [m for m in mons if m <= self.monitor_count]
        for m in mons:
            mon = self.monitors.get(m)
            if not mon:
                continue
            if mon["vlc_thread"] and mon["vlc_thread"].is_alive():
                vlc_embed.stop(m)
                mon["vlc_thread"].join(timeout=1)
                mon["vlc_thread"] = None
            if mon["playlist_thread"] and mon["playlist_thread"].is_alive():
                vlc_playlist.stop(m)
                mon["playlist_thread"].join(timeout=1)
                mon["playlist_thread"] = None
            if mon["playlist_path"]:
                try:
                    os.unlink(mon["playlist_path"])
                except FileNotFoundError:
                    pass
                mon["playlist_path"] = None
            vlc_embed.set_gui_images([], m)
            vlc_playlist.set_gui_images([], m)

    def _find_vnc_exe(self, name: str) -> Optional[pathlib.Path]:
        """Locate a VNC executable bundled with the application."""
        candidates = [
            BUNDLE_DIR / "sdk" / "vnc" / name,
            RUN_DIR / "sdk" / "vnc" / name,
            RUN_DIR / "etc" / "vncserver" / name,
        ]
        for path in candidates:
            if path.exists():
                return path
        return None

    def start_vnc_server(self, port: Optional[int], password: str) -> None:
        exe = self._find_vnc_exe("TightVncOpen.exe")
        if not exe:
            print("VNC server executable not found")
            return
        env = os.environ.copy()
        if port:
            env["VNC_PORT"] = str(port)
        try:
            subprocess.Popen(
                [str(exe), str(password), str(password)],
                cwd=str(exe.parent),
                env=env,
            )
            self.vnc_enabled = True
            self.vnc_port = port
            self.vnc_password = password
        except Exception as e:
            print(f"Failed to start VNC server: {e}")

    def stop_vnc_server(self) -> None:
        exe = self._find_vnc_exe("tvnserver.exe")
        if not exe:
            return
        try:
            subprocess.Popen([str(exe), "-kill"], cwd=str(exe.parent))
        except Exception as e:
            print(f"Failed to stop VNC server: {e}")
        self.vnc_enabled = False
        self.vnc_port = None
        self.vnc_password = None

    def launch_updater(self) -> None:
        exe = RUN_DIR / "EnterPlayer_AutoUpdate.exe"
        if not exe.exists():
            print(f"Updater not found: {exe}")
            return
        try:
            subprocess.Popen([str(exe)], cwd=str(RUN_DIR))
        except Exception as e:  # noqa: BLE001
            print(f"Failed to launch updater: {e}")

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
        """Download an audio file from ``url`` and play it."""
        try:
            async with httpx.AsyncClient(timeout=120) as cli:
                r = await cli.get(url)
                r.raise_for_status()

            parsed = urlparse(url)
            ext = os.path.splitext(parsed.path)[1].lower() or ".mp3"
            if ext not in {".mp3", ".wav"}:
                ext = ".mp3"

            tmp = tempfile.NamedTemporaryFile(delete=False, suffix=ext, dir=RUN_DIR)
            tmp.write(r.content)
            tmp.flush()
            tmp.close()

            if volume is not None:
                scheduler.set_volume(volume)

            scheduler.play_audio_file(tmp.name)
        except Exception as e:  # noqa: BLE001
            print(f"Failed to play audio from {url}: {e}")

    async def play_tts_text(
        self,
        text: str,
        *,
        speed: float = 1.0,
        pitch: float = 1.0,
        volume: Optional[int] = None,
    ) -> None:
        """Fetch TTS audio for ``text`` and play it."""
        audio = await scheduler.tts_request(text, speed=speed, pitch=pitch)
        if volume is not None:
            scheduler.set_volume(volume)
        scheduler.play_mp3(audio)

    async def connect_loop(self):
        backoff = 1
        while not self.stop_event.is_set():
            ws_url = HOST.replace("http", "ws") + (
                f"/ws?api_key={API_KEY}&device_id={self.device_id}&mac={MAC_ADDRESS}"
            )
            try:
                self.update_status("Connecting…")
                async with websockets.connect(
                    ws_url,
                    ping_interval=20,
                    ping_timeout=20,
                    close_timeout=5,
                    max_size=1 << 20,
                ) as ws:
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
                    vnc_enabled = data.get("VncEnabled")
                    vnc_password = data.get("VncPassword")
                    vnc_port = data.get("VncPort")
                    if isinstance(vnc_enabled, str):
                        vnc_enabled = vnc_enabled.lower() in {"1", "true", "yes"}
                    else:
                        vnc_enabled = bool(vnc_enabled)
                    try:
                        vnc_port = int(vnc_port) if vnc_port is not None else None
                    except Exception:
                        vnc_port = None
                    if vnc_enabled and vnc_password:
                        if (
                            not self.vnc_enabled
                            or self.vnc_port != vnc_port
                            or self.vnc_password != str(vnc_password)
                        ):
                            self.start_vnc_server(vnc_port, str(vnc_password))
                    else:
                        if self.vnc_enabled:
                            self.stop_vnc_server()
                    res = data.get("Resolution") or data.get("resolution")
                    orient = data.get("Orientation")
                    if res or orient is not None:
                        display_config.set_display_config(res, orient, monitor=1)
                    mon1 = data.get("Monitor1") or {}
                    mon2 = data.get("Monitor2") or {}
                    for idx, mon in ((1, mon1), (2, mon2)):
                        if idx > self.monitor_count:
                            continue
                        if not isinstance(mon, dict):
                            continue
                        r = mon.get("Resolution") or mon.get("resolution")
                        o = mon.get("Orientation")
                        if r or o is not None:
                            display_config.set_display_config(r, o, monitor=idx)
                        try:
                            if mon.get("VlcX") is not None:
                                self.monitors[idx]["vlc_x"] = int(float(mon.get("VlcX")))
                            if mon.get("VlcY") is not None:
                                self.monitors[idx]["vlc_y"] = int(float(mon.get("VlcY")))
                            if mon.get("VlcWidth") is not None:
                                self.monitors[idx]["vlc_width"] = int(float(mon.get("VlcWidth")))
                            if mon.get("VlcHeight") is not None:
                                self.monitors[idx]["vlc_height"] = int(float(mon.get("VlcHeight")))
                        except Exception:
                            pass
                    images = data.get("GuiImages")
                    if isinstance(images, list):
                        self.gui_images = list(images)
                        self.gui_images_by_monitor = {1: [], 2: []}
                        for info in images:
                            try:
                                m = int(info.get("Monitor", 1))
                            except Exception:
                                m = 1
                            if m not in (1, 2) or m > self.monitor_count:
                                m = 1
                            self.gui_images_by_monitor.setdefault(m, []).append(info)
                        for idx in range(1, self.monitor_count + 1):
                            vlc_embed.set_gui_images(
                                self.gui_images_by_monitor.get(idx, []), idx
                            )
                            vlc_playlist.set_gui_images(
                                self.gui_images_by_monitor.get(idx, []), idx
                            )
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
                            for idx in range(1, self.monitor_count + 1):
                                self.start_vlc(url, monitor=idx)
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
                elif isinstance(data, dict) and data.get("type") == "warning-broadcast":
                    wtype = int(data.get("warning_type", 0))
                    text = data.get("text", "")
                    volume = data.get("volume")
                    if wtype == 1:
                        asyncio.create_task(
                            self.play_tts_text(text, volume=volume)
                        )
                    elif wtype == 2 and text:
                        asyncio.create_task(self.play_audio_url(text, volume))
                elif isinstance(data, dict) and data.get("type") == "play-tts":
                    text = data.get("text", "")
                    speed = data.get("speed", 1.0)
                    pitch = data.get("pitch", 1.0)
                    volume = data.get("volume")
                    asyncio.create_task(
                        self.play_tts_text(
                            text, speed=speed, pitch=pitch, volume=volume
                        )
                    )
                elif isinstance(data, dict) and data.get("type") == "play-media":
                    mid = data.get("media_id")
                    if mid is not None:
                        mid_str = str(mid)
                        for m_idx, mon in self.monitors.items():
                            if m_idx > self.monitor_count:
                                continue
                            items = mon.get("playlist_items", [])
                            for i, it in enumerate(items):
                                if str(it.get("MediaID") or it.get("media_id") or it.get("id")) == mid_str:
                                    self.start_vlc_playlist(items, start_index=i, monitor=m_idx)
                                    break
                elif isinstance(data, dict) and data.get("type") == "playlist":
                    items = data.get("items")
                    if isinstance(items, list):
                        new_items = list(items)
                        
                        def item_id(it: dict) -> str:
                            return str(
                                it.get("MediaID")
                                or it.get("media_id")
                                or it.get("id")
                                or it.get("MediaUrl")
                                or it.get("url")
                            )

                        def item_vol(it: dict) -> str:
                            if "Volume" in it:
                                return str(it.get("Volume"))
                            if "volume" in it:
                                return str(it.get("volume"))
                            return ""

                        self.playlist_items = new_items
                        by_monitor = {1: [], 2: []}
                        for it in new_items:
                            m = int(it.get("Monitor", 1))
                            if m not in by_monitor:
                                m = 1
                            by_monitor[m].append(it)
                        for m_idx in range(1, self.monitor_count + 1):
                            new_list = by_monitor[m_idx]
                            old = self.monitors[m_idx]["playlist_items"]
                            same_order = (
                                len(old) == len(new_list)
                                and all(item_id(o) == item_id(n) for o, n in zip(old, new_list))
                            )
                            same_volume = (
                                same_order
                                and all(item_vol(o) == item_vol(n) for o, n in zip(old, new_list))
                            )
                            if same_order and same_volume:
                                continue
                            self.monitors[m_idx]["playlist_items"] = new_list
                            self.start_vlc_playlist(new_list, monitor=m_idx)
                elif isinstance(data, dict) and data.get("type") == "refresh-schedules":
                    await self.update_schedules(start_scheduler=self.playmode != 2)
                elif isinstance(data, dict) and data.get("type") == "update":
                    now = time.monotonic()
                    if now - self.last_update_time >= 30:
                        self.last_update_time = now
                        threading.Thread(target=self.launch_updater, daemon=True).start()
                    else:
                        print("Ignoring duplicate update command")
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
