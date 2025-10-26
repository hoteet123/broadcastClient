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
import logging

DEFAULT_URL = "http://nas.3no.kr/test.mp4"

import scheduler
import tempfile
from typing import Any, Callable, Coroutine, Optional
from urllib.parse import urlparse
import vlc_embed
import vlc_playlist
import display_config

HOST_URL = "https://api.flexx.kr:65000"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("enterplayer")

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
    def __init__(
        self,
        update_status: Callable[[str], None],
        *,
        on_schedules_updated: Optional[Callable[[list], None]] = None,
        on_message: Optional[Callable[[str, bool], None]] = None,
        on_test_broadcast_status: Optional[Callable[[Optional[int], bool, str], None]] = None,
    ) -> None:
        self.update_status = update_status
        self.on_schedules_updated = on_schedules_updated
        self.on_message = on_message
        self.on_test_broadcast_status = on_test_broadcast_status
        self.stop_event = threading.Event()
        self.thread = threading.Thread(target=self.run, daemon=True)
        self.scheduler_thread = None
        self.scheduler_stop_event = None
        self.schedules = []
        self.playlist_items = []
        self.device_id = DEVICE_ID
        self.device_enabled = True
        self.loop: Optional[asyncio.AbstractEventLoop] = None
        self.loop_ready = threading.Event()
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

    def _notify_schedules(self, schedules: list) -> None:
        if self.on_schedules_updated:
            try:
                self.on_schedules_updated(schedules)
            except Exception:  # noqa: BLE001
                logger.exception("Schedule update callback failed")

    def _notify_message(self, message: str, error: bool = False) -> None:
        if self.on_message:
            try:
                self.on_message(message, error)
            except Exception:  # noqa: BLE001
                logger.exception("Message callback failed")

    def _notify_test_status(
        self, schedule_id: Optional[int], success: bool, message: str
    ) -> None:
        if self.on_test_broadcast_status:
            try:
                self.on_test_broadcast_status(schedule_id, success, message)
            except Exception:  # noqa: BLE001
                logger.exception("Test status callback failed")

    def start(self):
        self.thread.start()

    def stop(self):
        self.stop_event.set()
        if self.scheduler_stop_event:
            self.scheduler_stop_event.set()
        if self.scheduler_thread and self.scheduler_thread.is_alive():
            self.scheduler_thread.join(timeout=1)
        self.stop_vlc()

    def _run_in_loop(
        self, coro: Coroutine[Any, Any, Any], description: str
    ) -> None:
        if not self.loop_ready.wait(timeout=5):
            self._notify_message(
                "네트워크 연결을 준비 중입니다. 잠시 후 다시 시도해 주세요.",
                error=True,
            )
            return
        loop = self.loop
        if not loop:
            self._notify_message("이벤트 루프가 초기화되지 않았습니다.", error=True)
            return
        future = asyncio.run_coroutine_threadsafe(coro, loop)

        def _on_done(fut: asyncio.Future) -> None:
            try:
                fut.result()
            except Exception:  # noqa: BLE001
                logger.exception("Error while running %s", description)
                self._notify_message(
                    f"작업 중 오류가 발생했습니다: {description}", error=True
                )

        future.add_done_callback(_on_done)

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
        self.loop = loop
        self.loop_ready.set()
        try:
            loop.run_until_complete(self.connect_loop())
        finally:
            self.loop_ready.clear()
            self.loop = None
            loop.close()

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
            except Exception as exc:  # noqa: BLE001
                logger.warning("WebSocket connection error: %s", exc)
                self.update_status(f"Disconnected: retry in {backoff}s")
                self._notify_message(
                    "서버 연결이 끊어져 재시도합니다.", error=True
                )
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 60)

    async def update_schedules(self, *, start_scheduler: Optional[bool] = None) -> None:
        """Fetch schedules from the server and update the scheduler list.

        ``start_scheduler`` defaults to ``True`` unless ``self.playmode`` is 2.
        """
        if start_scheduler is None:
            start_scheduler = self.playmode != 2
        try:
            schedules = await scheduler.fetch_schedules(cfg)
        except httpx.HTTPStatusError as exc:
            logger.error("Failed to fetch schedules: %s", exc)
            self._notify_message(
                "예약 정보를 불러오지 못했습니다. (서버 오류)", error=True
            )
            return
        except httpx.HTTPError as exc:
            logger.error("Failed to fetch schedules: %s", exc)
            self._notify_message(
                "네트워크 오류로 예약 정보를 불러오지 못했습니다.", error=True
            )
            return
        except Exception as exc:  # noqa: BLE001
            logger.exception("Unexpected error while fetching schedules: %s", exc)
            self._notify_message(
                "예약 정보를 불러오는 중 알 수 없는 오류가 발생했습니다.",
                error=True,
            )
            return
        if self.scheduler_thread and self.scheduler_thread.is_alive():
            if self.scheduler_stop_event:
                self.scheduler_stop_event.set()
            self.scheduler_thread.join(timeout=1)
            self.scheduler_thread = None

        self.schedules = list(schedules)
        logger.info("Fetched %d schedules", len(self.schedules))
        if self.schedules:
            logger.info(
                "Schedule fields: %s",
                ", ".join(sorted(str(k) for k in self.schedules[0].keys())),
            )
        self._notify_schedules(self.schedules)
        if self.schedules:
            self._notify_message(
                f"예약 {len(self.schedules)}건을 불러왔습니다.", error=False
            )
        else:
            self._notify_message("등록된 예약이 없습니다.", error=False)

        if start_scheduler and self.schedules and self.device_enabled:
            self.scheduler_stop_event = threading.Event()
            self.scheduler_thread = threading.Thread(
                target=scheduler.run,
                args=(self.schedules, self.scheduler_stop_event),
                daemon=True,
            )
            self.scheduler_thread.start()

    def refresh_schedules(self) -> None:
        self._notify_message("예약 목록을 새로 고치는 중입니다…", error=False)
        self._run_in_loop(
            self.update_schedules(start_scheduler=None), "refresh schedules"
        )

    def request_test_broadcast(self, schedule_id: Optional[int]) -> None:
        if schedule_id is None:
            self._notify_message("선택된 예약이 없습니다.", error=True)
            return
        try:
            sid = int(schedule_id)
        except (TypeError, ValueError):
            self._notify_message("잘못된 예약 ID 입니다.", error=True)
            return
        self._notify_message("테스트 방송을 요청하는 중입니다…", error=False)
        self._run_in_loop(
            self._post_test_broadcast(sid), f"test broadcast request #{sid}"
        )

    async def _post_test_broadcast(self, schedule_id: int) -> None:
        logger.info("Requesting test broadcast for schedule %s", schedule_id)
        headers = {"X-API-Key": API_KEY}
        params = {"mac": MAC_ADDRESS}
        try:
            async with httpx.AsyncClient(
                base_url=HOST, http2=True, timeout=10.0
            ) as cli:
                response = await cli.post(
                    f"/broadcast-schedules/{schedule_id}/test",
                    params=params,
                    headers=headers,
                )
                response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            logger.error("Test broadcast request failed: %s", exc)
            self._notify_test_status(
                schedule_id, False, "테스트 방송 요청이 거절되었습니다."
            )
            self._notify_message("테스트 방송 요청이 거절되었습니다.", error=True)
        except httpx.HTTPError as exc:
            logger.error("Test broadcast request failed: %s", exc)
            self._notify_test_status(
                schedule_id, False, "테스트 방송 요청 중 네트워크 오류가 발생했습니다."
            )
            self._notify_message(
                "네트워크 오류로 테스트 방송 요청에 실패했습니다.", error=True
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception("Unexpected error during test broadcast request: %s", exc)
            self._notify_test_status(
                schedule_id, False, "테스트 방송 요청 중 알 수 없는 오류가 발생했습니다."
            )
            self._notify_message(
                "테스트 방송 요청 중 오류가 발생했습니다.", error=True
            )
        else:
            self._notify_test_status(
                schedule_id, True, "테스트 방송 요청을 서버에 전달했습니다."
            )
            self._notify_message("테스트 방송 요청을 전송했습니다.", error=False)

    def request_play_tts(self, text: str) -> None:
        text = text.strip()
        if not text:
            self._notify_message("재생할 문장을 입력해 주세요.", error=True)
            return
        if len(text) > 2000:
            self._notify_message("문장이 너무 깁니다. 2000자 이하로 입력해 주세요.", error=True)
            return
        self._notify_message("즉시 재생 TTS를 요청하는 중입니다…", error=False)
        self._run_in_loop(self._post_play_tts(text), "play TTS request")

    async def _post_play_tts(self, text: str) -> None:
        logger.info("Requesting immediate TTS playback (%d chars)", len(text))
        headers = {
            "X-API-Key": API_KEY,
            "Content-Type": "text/plain; charset=utf-8",
        }
        params = {"mac": MAC_ADDRESS}
        try:
            async with httpx.AsyncClient(
                base_url=HOST, http2=True, timeout=10.0
            ) as cli:
                response = await cli.post(
                    "/broadcasts/play-tts",
                    params=params,
                    headers=headers,
                    content=text.encode("utf-8"),
                )
                response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            logger.error("Immediate TTS request failed: %s", exc)
            self._notify_message(
                "TTS 재생 요청이 거절되었습니다. 입력 값을 확인해 주세요.",
                error=True,
            )
        except httpx.HTTPError as exc:
            logger.error("Immediate TTS request failed: %s", exc)
            self._notify_message(
                "네트워크 오류로 TTS 재생을 요청하지 못했습니다.", error=True
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception("Unexpected error during TTS request: %s", exc)
            self._notify_message("TTS 재생 요청 중 오류가 발생했습니다.", error=True)
        else:
            self._notify_message("TTS 재생 요청을 전송했습니다.", error=False)

    async def handle_ws(self, ws):
        try:
            await ws.send(json.dumps({"hello": "world", "mac": MAC_ADDRESS}))
            logger.info("Sent hello handshake over WebSocket")

            # 서버 설정을 받은 뒤 스케줄을 불러온다

            while not self.stop_event.is_set():
                msg = await ws.recv()
                logger.debug("[WS] raw message: %s", msg)
                try:
                    data = json.loads(msg)
                except json.JSONDecodeError:
                    try:
                        data = ast.literal_eval(msg)
                    except Exception:
                        logger.warning("[WS] Unparseable message: %s", msg)
                        continue
                except Exception:
                    logger.warning("[WS] Failed to decode message: %s", msg)
                    continue

                if isinstance(data, dict):
                    msg_type = data.get("type")
                else:
                    msg_type = None

                if isinstance(data, dict) and msg_type == "rename":
                    new_id = data.get("device_id")
                    if new_id:
                        self.device_id = new_id
                        cfg["DEVICE_ID"] = new_id
                        save_config(cfg)
                        self.update_status(f"Renamed to {new_id}")
                        logger.info("Device renamed to %s", new_id)
                elif isinstance(data, dict) and msg_type == "config":
                    logger.info("Received configuration payload")
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
                elif isinstance(data, dict) and msg_type == "test-broadcast":
                    sid = data.get("schedule_id")
                    logger.info("Received test-broadcast command for schedule %s", sid)
                    sch = next((s for s in self.schedules if s.get("ScheduleID") == sid), None)
                    if sch:
                        asyncio.create_task(self.play_schedule(sch))
                        self._notify_test_status(
                            sid,
                            True,
                            "서버에서 테스트 방송 명령을 수신했습니다.",
                        )
                    else:
                        logger.warning("Received test-broadcast for unknown schedule %s", sid)
                        self._notify_test_status(
                            sid,
                            False,
                            "테스트 방송 대상 예약을 찾을 수 없습니다.",
                        )
                elif isinstance(data, dict) and msg_type == "custom-broadcast":
                    url = data.get("audio_url")
                    volume = data.get("volume")
                    if url:
                        asyncio.create_task(self.play_audio_url(url, volume))
                elif isinstance(data, dict) and msg_type == "warning-broadcast":
                    wtype = int(data.get("warning_type", 0))
                    text = data.get("text", "")
                    volume = data.get("volume")
                    if wtype == 1:
                        asyncio.create_task(
                            self.play_tts_text(text, volume=volume)
                        )
                    elif wtype == 2 and text:
                        asyncio.create_task(self.play_audio_url(text, volume))
                elif isinstance(data, dict) and msg_type == "play-tts":
                    text = data.get("text", "")
                    speed = data.get("speed", 1.0)
                    pitch = data.get("pitch", 1.0)
                    volume = data.get("volume")
                    logger.info(
                        "Received play-tts command (len=%d)", len(text or "")
                    )
                    asyncio.create_task(
                        self.play_tts_text(
                            text, speed=speed, pitch=pitch, volume=volume
                        )
                    )
                elif isinstance(data, dict) and msg_type == "play-media":
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
                elif isinstance(data, dict) and msg_type == "playlist":
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
                elif isinstance(data, dict) and msg_type == "refresh-schedules":
                    logger.info("Received refresh-schedules command")
                    self._notify_message("서버 요청으로 예약 목록을 갱신합니다.", False)
                    await self.update_schedules(start_scheduler=self.playmode != 2)
                elif isinstance(data, dict) and msg_type == "update":
                    now = time.monotonic()
                    if now - self.last_update_time >= 30:
                        self.last_update_time = now
                        threading.Thread(target=self.launch_updater, daemon=True).start()
                    else:
                        print("Ignoring duplicate update command")
                else:
                    logger.info("Unhandled WS message: %s", data)
        except ConnectionClosed as exc:
            logger.warning("WebSocket closed: %s", exc)
            self._notify_message("서버와의 연결이 종료되었습니다.", error=True)
        except Exception as exc:  # noqa: BLE001
            logger.exception("WebSocket handler error: %s", exc)
            self._notify_message("서버와의 통신 중 오류가 발생했습니다.", error=True)


def main():
    root = tk.Tk()
    root.title("방송 플레이어 클라이언트")

    status_var = tk.StringVar(value="시작 중…")
    message_var = tk.StringVar(value="")

    status_label = tk.Label(root, textvariable=status_var, anchor="w")
    status_label.pack(fill="x", padx=20, pady=(20, 5))

    message_label = tk.Label(root, textvariable=message_var, anchor="w", fg="#333333")
    message_label.pack(fill="x", padx=20, pady=(0, 10))

    schedule_frame = tk.LabelFrame(root, text="예약 목록")
    schedule_frame.pack(fill="both", expand=True, padx=20, pady=(0, 10))

    schedule_listbox = tk.Listbox(schedule_frame, height=8, activestyle="dotbox")
    schedule_listbox.pack(side="left", fill="both", expand=True, padx=(10, 0), pady=10)

    schedule_scrollbar = tk.Scrollbar(schedule_frame, orient="vertical")
    schedule_scrollbar.pack(side="right", fill="y", padx=(0, 10), pady=10)
    schedule_listbox.config(yscrollcommand=schedule_scrollbar.set)
    schedule_scrollbar.config(command=schedule_listbox.yview)

    button_frame = tk.Frame(root)
    button_frame.pack(fill="x", padx=20, pady=(0, 10))

    test_button = tk.Button(
        button_frame,
        text="선택한 예약 테스트 방송",
        state="disabled",
    )
    test_button.pack(side="left")

    refresh_button = tk.Button(button_frame, text="예약 새로고침")
    refresh_button.pack(side="right")

    tts_frame = tk.LabelFrame(root, text="즉시 TTS 재생")
    tts_frame.pack(fill="both", expand=False, padx=20, pady=(0, 20))

    tts_text = tk.Text(tts_frame, height=4, wrap="word")
    tts_text.pack(fill="both", expand=True, padx=10, pady=(10, 5))

    tts_button = tk.Button(tts_frame, text="TTS 재생 요청")
    tts_button.pack(anchor="e", padx=10, pady=(0, 10))

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

    schedule_items = []
    client_ref = {"client": None}

    def set_message(text: str, error: bool = False) -> None:
        def _apply() -> None:
            color = "#d32f2f" if error else ("#2e7d32" if text else "#333333")
            message_label.config(fg=color)
            message_var.set(text)

        root.after(0, _apply)

    def update_status(text: str) -> None:
        root.after(0, status_var.set, text)

    def handle_message(message: str, error: bool) -> None:
        set_message(message, error)

    def handle_schedules(schedules: list) -> None:
        def _apply() -> None:
            nonlocal schedule_items
            schedule_items = list(schedules)
            schedule_listbox.delete(0, tk.END)
            for sch in schedule_items:
                sid = sch.get("ScheduleID") or sch.get("schedule_id")
                title = sch.get("Title") or sch.get("title") or "(제목 없음)"
                time_str = sch.get("ScheduledTime") or sch.get("scheduled_time") or ""
                display = f"[{sid}] {title}" if sid is not None else title
                if time_str:
                    display += f" - {time_str}"
                schedule_listbox.insert(tk.END, display)
            update_test_button_state()

        root.after(0, _apply)

    def handle_test_status(
        schedule_id: Optional[int], success: bool, message: str
    ) -> None:
        set_message(message, error=not success)

    def update_test_button_state(event=None) -> None:  # noqa: ANN001
        state = "normal" if schedule_listbox.curselection() else "disabled"
        test_button.config(state=state)

    def refresh_schedules() -> None:
        client = client_ref["client"]
        if client:
            client.refresh_schedules()

    def send_test_broadcast() -> None:
        selection = schedule_listbox.curselection()
        if not selection:
            set_message("예약을 먼저 선택해 주세요.", error=True)
            return
        schedule = schedule_items[selection[0]] if selection else None
        if not schedule:
            set_message("선택한 예약 정보를 찾을 수 없습니다.", error=True)
            return
        schedule_id = schedule.get("ScheduleID") or schedule.get("schedule_id")
        client = client_ref["client"]
        if client:
            client.request_test_broadcast(schedule_id)

    def send_tts_request() -> None:
        text = tts_text.get("1.0", tk.END)
        client = client_ref["client"]
        if client:
            client.request_play_tts(text)

    schedule_listbox.bind("<<ListboxSelect>>", update_test_button_state)
    test_button.config(command=send_test_broadcast)
    refresh_button.config(command=refresh_schedules)
    tts_button.config(command=send_tts_request)

    def on_close():
        client = client_ref["client"]
        if client:
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

    client = WSClient(
        update_status,
        on_schedules_updated=handle_schedules,
        on_message=handle_message,
        on_test_broadcast_status=handle_test_status,
    )
    client_ref["client"] = client
    client.start()

    if icon:
        threading.Thread(target=icon.run, daemon=True).start()

    root.protocol("WM_DELETE_WINDOW", on_close)
    root.mainloop()


if __name__ == "__main__":
    main()
