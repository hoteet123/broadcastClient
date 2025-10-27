import asyncio
import contextlib
import json
import logging
import threading
import sys
import tkinter as tk
import uuid
import ast
import pathlib
import os
import subprocess
import time
from tkinter import ttk


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger("broadcast_client")

DEFAULT_URL = "http://nas.3no.kr/test.mp4"

import scheduler
import tempfile
from typing import Any, Callable, Coroutine, Dict, List, Optional
from urllib.parse import urlparse
import vlc_embed
import vlc_playlist
import display_config

HOST_URL = "https://api.flexx.kr:65000"

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
    logger.warning("pystray not available: %s. Running without system tray icon.", e)
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
        logger.info("Created %s. Fill in API_KEY and run again.", CFG_PATH)
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
        update_status,
        *,
        on_schedules_updated=None,
        on_schedules_error=None,
        on_test_broadcast=None,
        on_remote_tts=None,
        log_callback=None,
    ):
        self.update_status = update_status
        self.on_schedules_updated = on_schedules_updated or (lambda schedules: None)
        self.on_schedules_error = on_schedules_error or (lambda message: None)
        self.on_test_broadcast = on_test_broadcast or (
            lambda schedule_id, payload=None: None
        )
        self.on_remote_tts = on_remote_tts or (lambda text: None)
        self.log_callback = log_callback
        self.stop_event = threading.Event()
        self.thread = threading.Thread(target=self.run, daemon=True)
        self.scheduler_thread = None
        self.scheduler_stop_event = None
        self.schedules = []
        self.playlist_items = []
        self.device_id = DEVICE_ID
        self.device_enabled = True
        self.loop = None
        self._current_ws = None
        self._logged_schedule_schema = False
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

    def start(self):
        self.thread.start()
        self.log("웹소켓 수신 스레드를 시작했습니다.")

    def stop(self):
        self.stop_event.set()
        if self.loop and self.loop.is_running():
            asyncio.run_coroutine_threadsafe(self._close_active_ws(), self.loop)
        if self.scheduler_stop_event:
            self.scheduler_stop_event.set()
        if self.scheduler_thread and self.scheduler_thread.is_alive():
            self.scheduler_thread.join(timeout=1)
        self.stop_vlc()
        if self.thread.is_alive():
            self.thread.join(timeout=2)

    def submit_async(self, coro: Coroutine[Any, Any, Any]) -> "asyncio.Future":
        """Run ``coro`` in the client's event loop from another thread."""
        if not self.loop_ready.wait(timeout=5):
            raise RuntimeError("이벤트 루프 초기화 대기 중에 시간이 초과되었습니다.")
        if not self.loop:
            raise RuntimeError("이벤트 루프가 초기화되지 않았습니다.")
        return asyncio.run_coroutine_threadsafe(coro, self.loop)

    async def _close_active_ws(self) -> None:
        ws = self._current_ws
        if ws and not ws.closed:
            try:
                await ws.close()
            except Exception as exc:  # noqa: BLE001
                self.log(f"웹소켓 종료 중 오류: {exc}", logging.WARNING)

    def log(self, message: str, level: int = logging.INFO) -> None:
        logger.log(level, message)
        if self.log_callback:
            try:
                self.log_callback(message, level)
            except Exception:  # noqa: BLE001
                logger.debug("Failed to forward log message to callback", exc_info=True)

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
            self.log(f"Updater not found: {exe}", logging.WARNING)
            return
        try:
            subprocess.Popen([str(exe)], cwd=str(RUN_DIR))
        except Exception as e:  # noqa: BLE001
            self.log(f"Failed to launch updater: {e}", logging.ERROR)

    def run(self):
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        self.loop = loop
        self.loop_ready.set()
        try:
            loop.run_until_complete(self.connect_loop())
        except Exception as exc:  # noqa: BLE001
            self.log(f"이벤트 루프 실행 중 오류 발생: {exc}", logging.ERROR)
        finally:
            pending = asyncio.all_tasks(loop)
            for task in pending:
                task.cancel()
            if pending:
                with contextlib.suppress(Exception):
                    loop.run_until_complete(
                        asyncio.gather(*pending, return_exceptions=True)
                    )
            loop.close()
            self.loop = None
            self.loop_ready.clear()

    async def play_schedule(self, sch: dict) -> None:
        """Fetch TTS audio for a schedule and play it."""
        volume = sch.get("Volume")
        if volume is None:
            volume = sch.get("volume")
        if volume is not None:
            try:
                scheduler.set_volume(int(float(volume)))
            except Exception as exc:  # noqa: BLE001
                self.log(
                    f"Failed to set schedule volume {volume!r}: {exc}",
                    logging.WARNING,
                )
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
            self.log(f"Failed to play audio from {url}: {e}", logging.ERROR)

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

    async def request_test_broadcast(self, schedule_id: int) -> Any:
        url = f"/broadcast-schedules/{schedule_id}/test"
        headers = {"X-API-Key": API_KEY}
        params = {"mac": MAC_ADDRESS}
        try:
            async with httpx.AsyncClient(
                base_url=HOST, timeout=10.0, http2=True
            ) as cli:
                response = await cli.post(url, params=params, headers=headers)
                response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            message = (
                "테스트 방송 요청이 거부되었습니다. 서버 응답 코드: "
                f"{exc.response.status_code}"
            )
            detail = exc.response.text.strip()
            if detail:
                message += f" ({detail[:200]})"
            self.log(message, logging.ERROR)
            raise RuntimeError(message) from exc
        except httpx.RequestError as exc:
            message = (
                "네트워크 오류로 테스트 방송 요청을 전송하지 못했습니다. "
                "인터넷 연결을 확인해 주세요."
            )
            self.log(f"{message} ({exc})", logging.ERROR)
            raise RuntimeError(message) from exc
        except Exception as exc:  # noqa: BLE001
            message = "알 수 없는 오류로 테스트 방송 요청에 실패했습니다."
            self.log(f"{message} ({exc})", logging.ERROR)
            raise RuntimeError(message) from exc

        self.log(f"테스트 방송 요청을 전송했습니다. schedule_id={schedule_id}")
        try:
            return response.json()
        except ValueError:
            return response.text.strip()

    async def request_play_tts(self, text: str) -> Any:
        url = "/broadcasts/play-tts"
        headers = {
            "X-API-Key": API_KEY,
            "Content-Type": "text/plain; charset=utf-8",
        }
        params = {"mac": MAC_ADDRESS}
        try:
            async with httpx.AsyncClient(
                base_url=HOST, timeout=10.0, http2=True
            ) as cli:
                response = await cli.post(
                    url,
                    params=params,
                    headers=headers,
                    content=text.encode("utf-8"),
                )
                response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            message = (
                "즉시 TTS 재생 요청이 거부되었습니다. 서버 응답 코드: "
                f"{exc.response.status_code}"
            )
            detail = exc.response.text.strip()
            if detail:
                message += f" ({detail[:200]})"
            self.log(message, logging.ERROR)
            raise RuntimeError(message) from exc
        except httpx.RequestError as exc:
            message = (
                "네트워크 오류로 즉시 TTS 재생을 요청하지 못했습니다. "
                "인터넷 연결을 확인해 주세요."
            )
            self.log(f"{message} ({exc})", logging.ERROR)
            raise RuntimeError(message) from exc
        except Exception as exc:  # noqa: BLE001
            message = "알 수 없는 오류로 TTS 재생 요청에 실패했습니다."
            self.log(f"{message} ({exc})", logging.ERROR)
            raise RuntimeError(message) from exc

        self.log("즉시 TTS 재생 요청을 전송했습니다.")
        try:
            return response.json()
        except ValueError:
            return response.text.strip()

    async def connect_loop(self):
        backoff = 1
        while not self.stop_event.is_set():
            ws_url = HOST.replace("http", "ws") + (
                f"/ws?api_key={API_KEY}&device_id={self.device_id}&mac={MAC_ADDRESS}"
            )
            try:
                self.update_status("Connecting…")
                self.log("웹소켓 서버에 연결을 시도합니다.")
                async with websockets.connect(
                    ws_url,
                    ping_interval=20,
                    ping_timeout=20,
                    close_timeout=5,
                    max_size=1 << 20,
                ) as ws:
                    self.update_status("Connected")
                    self.log("웹소켓 서버에 연결되었습니다.")
                    backoff = 1
                    await self.handle_ws(ws)
            except Exception as exc:  # noqa: BLE001
                if self.stop_event.is_set():
                    break
                self.log(f"웹소켓 연결 오류: {exc}", logging.WARNING)
                self.update_status(f"Disconnected: retry in {backoff}s")
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
            self.log(f"스케줄 {len(schedules)}건을 불러왔습니다.")
            if (
                schedules
                and not self._logged_schedule_schema
                and isinstance(schedules[0], dict)
            ):
                keys = sorted(schedules[0].keys())
                self.log(
                    "서버 스케줄 필드 목록: " + ", ".join(keys),
                    logging.DEBUG,
                )
                self._logged_schedule_schema = True
        except httpx.HTTPStatusError as exc:
            message = (
                "스케줄 조회에 실패했습니다. 서버 응답 코드: "
                f"{exc.response.status_code}"
            )
            detail = exc.response.text.strip()
            if detail:
                message += f" ({detail[:200]})"
            self.log(message, logging.ERROR)
            self.on_schedules_error(message)
            return
        except httpx.RequestError as exc:
            message = (
                "네트워크 오류로 스케줄을 가져오지 못했습니다. "
                "인터넷 연결을 확인해 주세요."
            )
            self.log(f"{message} ({exc})", logging.ERROR)
            self.on_schedules_error(message)
            return
        except Exception as exc:  # noqa: BLE001
            message = "알 수 없는 오류로 스케줄 동기화에 실패했습니다."
            self.log(f"{message} ({exc})", logging.ERROR)
            self.on_schedules_error(message)
            return
        if self.scheduler_thread and self.scheduler_thread.is_alive():
            if self.scheduler_stop_event:
                self.scheduler_stop_event.set()
            self.scheduler_thread.join(timeout=1)
            self.scheduler_thread = None

        self.schedules = list(schedules)
        self.on_schedules_updated(self.schedules)

        if start_scheduler and self.schedules and self.device_enabled:
            self.scheduler_stop_event = threading.Event()
            self.scheduler_thread = threading.Thread(
                target=scheduler.run,
                args=(self.schedules, self.scheduler_stop_event),
                daemon=True,
            )
            self.scheduler_thread.start()

    async def handle_ws(self, ws):
        self._current_ws = ws
        try:
            hello_payload = {"hello": "world", "mac": MAC_ADDRESS}
            await ws.send(json.dumps(hello_payload))
            self.log(f"초기 핸드셰이크 메시지를 전송했습니다: {hello_payload}", logging.DEBUG)

            while not self.stop_event.is_set():
                msg = await ws.recv()
                self.log(f"웹소켓 수신 원문: {msg}", logging.DEBUG)
                try:
                    data = json.loads(msg)
                except json.JSONDecodeError:
                    try:
                        data = ast.literal_eval(msg)
                    except Exception:
                        self.log(f"알 수 없는 웹소켓 메시지 형식: {msg}", logging.WARNING)
                        continue
                except Exception as exc:  # noqa: BLE001
                    self.log(f"웹소켓 메시지 파싱 실패: {exc}", logging.WARNING)
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
                    self.log(
                        f"테스트 방송 명령을 수신했습니다. schedule_id={sid}",
                        logging.INFO,
                    )
                    self.on_test_broadcast(sid, data)
                    sch = next((s for s in self.schedules if s.get("ScheduleID") == sid), None)
                    if sch:
                        asyncio.create_task(self.play_schedule(sch))
                    else:
                        self.log(
                            f"schedule_id={sid} 에 해당하는 예약을 찾지 못했습니다.",
                            logging.WARNING,
                        )
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
                    preview = text if len(text) < 50 else text[:47] + "…"
                    self.log(
                        f"즉시 TTS 재생 명령 수신: '{preview}'",
                        logging.INFO,
                    )
                    self.on_remote_tts(text)
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
                    self.log("스케줄 새로고침 명령을 수신했습니다.")
                    await self.update_schedules(start_scheduler=self.playmode != 2)
                elif isinstance(data, dict) and data.get("type") == "update":
                    now = time.monotonic()
                    if now - self.last_update_time >= 30:
                        self.last_update_time = now
                        threading.Thread(target=self.launch_updater, daemon=True).start()
                    else:
                        self.log("Ignoring duplicate update command", logging.DEBUG)
                else:
                    self.log(f"처리되지 않은 웹소켓 메시지: {data}", logging.DEBUG)
        except ConnectionClosed as exc:
            self.log(f"웹소켓 연결이 종료되었습니다: {exc}")
        except Exception as exc:  # noqa: BLE001
            self.log(f"웹소켓 처리 중 오류가 발생했습니다: {exc}", logging.ERROR)
        finally:
            self._current_ws = None


def main():
    root = tk.Tk()
    root.title("방송 플레이어 클라이언트")
    root.geometry("900x720")

    try:
        ttk.Style().theme_use("clam")
    except tk.TclError:
        pass

    status_var = tk.StringVar(value="연결 준비 중…")
    operation_var = tk.StringVar(value="")
    schedule_count_var = tk.StringVar(value="예약 정보를 불러오는 중입니다…")
    schedule_status_var = tk.StringVar(value="")
    tts_status_var = tk.StringVar(value="")

    main_frame = ttk.Frame(root, padding=12)
    main_frame.pack(fill="both", expand=True)
    main_frame.columnconfigure(0, weight=1)
    main_frame.rowconfigure(2, weight=3)
    main_frame.rowconfigure(3, weight=2)
    main_frame.rowconfigure(4, weight=2)

    ttk.Label(
        main_frame,
        textvariable=status_var,
        font=("맑은 고딕", 12, "bold"),
        anchor="w",
    ).grid(row=0, column=0, sticky="ew")
    ttk.Label(
        main_frame,
        textvariable=operation_var,
        foreground="#0057b7",
        anchor="w",
        wraplength=860,
    ).grid(row=1, column=0, sticky="ew", pady=(6, 0))

    schedules: List[Dict[str, Any]] = []
    schedule_statuses: Dict[int, str] = {}
    selected_schedule_id: Optional[int] = None

    def get_schedule_id(schedule: Dict[str, Any]) -> Optional[int]:
        sid = (
            schedule.get("ScheduleID")
            or schedule.get("schedule_id")
            or schedule.get("id")
        )
        if sid is None:
            return None
        try:
            return int(sid)
        except (TypeError, ValueError):
            try:
                return int(float(sid))
            except Exception:  # noqa: BLE001
                return None

    def format_schedule_item(schedule: Dict[str, Any]) -> str:
        sid = get_schedule_id(schedule)
        title = schedule.get("Title") or schedule.get("title") or "(제목 없음)"
        time_str = schedule.get("ScheduledTime") or schedule.get("scheduled_time")
        date_str = schedule.get("ScheduledDate") or schedule.get("scheduled_date")
        prefix = f"[{sid}] " if sid is not None else ""
        if date_str:
            return f"{prefix}{date_str} {time_str or ''} - {title}"
        if time_str:
            return f"{prefix}{time_str} - {title}"
        return f"{prefix}{title}"

    def set_schedule_status(schedule_id: Optional[int], message: str) -> None:
        if schedule_id is None:
            return
        schedule_statuses[schedule_id] = message
        if selected_schedule_id == schedule_id:
            schedule_status_var.set(message)

    def render_schedule_detail(schedule: Optional[Dict[str, Any]]) -> None:
        text_widget.configure(state="normal")
        text_widget.delete("1.0", tk.END)
        if schedule:
            detail = json.dumps(schedule, ensure_ascii=False, indent=2)
            text_widget.insert("1.0", detail)
        else:
            text_widget.insert("1.0", "예약을 선택하면 상세 정보가 표시됩니다.")
        text_widget.configure(state="disabled")

        if schedule:
            sid = get_schedule_id(schedule)
            if sid is not None and sid in schedule_statuses:
                schedule_status_var.set(schedule_statuses[sid])
            else:
                schedule_status_var.set("테스트 방송 이력이 없습니다.")
        else:
            schedule_status_var.set("")

    def append_log(message: str, level: int = logging.INFO) -> None:
        prefix = {
            logging.DEBUG: "DEBUG",
            logging.INFO: "INFO",
            logging.WARNING: "WARN",
            logging.ERROR: "ERROR",
            logging.CRITICAL: "CRIT",
        }.get(level, "INFO")
        timestamp = time.strftime("%H:%M:%S")
        log_line = f"[{timestamp}] {prefix} {message}\n"
        log_text.configure(state="normal")
        log_text.insert(tk.END, log_line)
        log_text.see(tk.END)
        log_text.configure(state="disabled")

    schedule_frame = ttk.LabelFrame(main_frame, text="방송 예약 목록", padding=8)
    schedule_frame.grid(row=2, column=0, sticky="nsew", pady=(12, 12))
    schedule_frame.columnconfigure(0, weight=1)
    schedule_frame.columnconfigure(1, weight=1)
    schedule_frame.rowconfigure(1, weight=1)

    header_frame = ttk.Frame(schedule_frame)
    header_frame.grid(row=0, column=0, columnspan=2, sticky="ew")
    header_frame.columnconfigure(0, weight=1)
    ttk.Label(
        header_frame,
        textvariable=schedule_count_var,
        anchor="w",
    ).grid(row=0, column=0, sticky="w")

    def refresh_schedules(event: Optional[tk.Event] = None) -> None:  # noqa: ARG001
        operation_var.set("스케줄을 불러오는 중입니다…")
        try:
            future = client.submit_async(client.update_schedules())
        except RuntimeError as exc:
            operation_var.set(str(exc))
            return

        def done(fut: "asyncio.Future") -> None:
            exc = fut.exception()
            if exc:
                root.after(0, operation_var.set, str(exc))
            else:
                root.after(0, operation_var.set, "스케줄을 최신 상태로 갱신했습니다.")

        future.add_done_callback(done)

    ttk.Button(header_frame, text="새로고침", command=refresh_schedules).grid(
        row=0, column=1, sticky="e"
    )

    list_frame = ttk.Frame(schedule_frame)
    list_frame.grid(row=1, column=0, sticky="nsew", padx=(0, 8))
    list_frame.columnconfigure(0, weight=1)
    list_frame.rowconfigure(0, weight=1)

    schedule_listbox = tk.Listbox(
        list_frame,
        selectmode=tk.SINGLE,
        activestyle="dotbox",
        exportselection=False,
    )
    scrollbar = ttk.Scrollbar(list_frame, orient="vertical", command=schedule_listbox.yview)
    schedule_listbox.configure(yscrollcommand=scrollbar.set)
    schedule_listbox.grid(row=0, column=0, sticky="nsew")
    scrollbar.grid(row=0, column=1, sticky="ns")

    detail_frame = ttk.Frame(schedule_frame)
    detail_frame.grid(row=1, column=1, sticky="nsew")
    detail_frame.columnconfigure(0, weight=1)
    detail_frame.rowconfigure(1, weight=1)

    ttk.Label(detail_frame, text="선택한 예약 정보", font=("맑은 고딕", 11, "bold")).grid(
        row=0, column=0, sticky="w"
    )
    text_widget = tk.Text(
        detail_frame,
        height=16,
        wrap="word",
        state="disabled",
    )
    text_widget.grid(row=1, column=0, sticky="nsew", pady=4)

    ttk.Label(
        detail_frame,
        textvariable=schedule_status_var,
        foreground="#444",
        wraplength=360,
    ).grid(row=2, column=0, sticky="ew", pady=(4, 8))

    render_schedule_detail(None)

    def trigger_test_broadcast() -> None:
        nonlocal selected_schedule_id
        selection = schedule_listbox.curselection()
        if not selection:
            operation_var.set("테스트 방송을 실행할 예약을 선택해 주세요.")
            return
        index = selection[0]
        schedule = schedules[index]
        schedule_id = get_schedule_id(schedule)
        if schedule_id is None:
            operation_var.set("해당 예약에는 ScheduleID가 없어 테스트 방송을 요청할 수 없습니다.")
            return

        selected_schedule_id = schedule_id
        request_time = time.strftime("%H:%M:%S")
        set_schedule_status(
            schedule_id,
            f"{request_time} 테스트 방송 요청을 전송했습니다. 서버 응답을 기다리는 중입니다.",
        )
        operation_var.set("테스트 방송 요청을 서버에 전송했습니다.")

        try:
            future = client.submit_async(client.request_test_broadcast(schedule_id))
        except RuntimeError as exc:
            message = str(exc)
            operation_var.set(message)
            failure_time = time.strftime("%H:%M:%S")
            set_schedule_status(
                schedule_id,
                f"{failure_time} 테스트 방송 요청 실패: {message}",
            )
            return

        def done(fut: "asyncio.Future") -> None:
            exc = fut.exception()
            if exc:
                message = str(exc)
                failure_time = time.strftime("%H:%M:%S")
                root.after(
                    0,
                    lambda: (
                        operation_var.set(message),
                        set_schedule_status(
                            schedule_id,
                            f"{failure_time} 테스트 방송 요청 실패: {message}",
                        ),
                    ),
                )
            else:
                success_time = time.strftime("%H:%M:%S")
                root.after(
                    0,
                    lambda: (
                        operation_var.set(
                            "테스트 방송 요청이 접수되었습니다. 방송 장치에서 응답을 기다리는 중입니다."
                        ),
                        set_schedule_status(
                            schedule_id,
                            f"{success_time} 테스트 방송 요청이 정상적으로 접수되었습니다.",
                        ),
                    ),
                )

        future.add_done_callback(done)

    ttk.Button(
        detail_frame,
        text="선택한 예약 테스트 방송",
        command=trigger_test_broadcast,
    ).grid(row=3, column=0, sticky="e", pady=(4, 0))

    tts_frame = ttk.LabelFrame(main_frame, text="즉시 TTS 재생", padding=8)
    tts_frame.grid(row=3, column=0, sticky="nsew")
    tts_frame.columnconfigure(0, weight=1)

    tts_text = tk.Text(tts_frame, height=5, wrap="word")
    tts_text.grid(row=0, column=0, sticky="nsew")

    tts_button_frame = ttk.Frame(tts_frame)
    tts_button_frame.grid(row=1, column=0, sticky="ew", pady=(6, 0))
    tts_button_frame.columnconfigure(0, weight=1)

    def send_tts() -> None:
        text = tts_text.get("1.0", tk.END).strip()
        if not text:
            tts_status_var.set("재생할 문장을 입력해 주세요.")
            return
        tts_status_var.set("TTS 재생 요청을 전송하는 중입니다…")

        try:
            future = client.submit_async(client.request_play_tts(text))
        except RuntimeError as exc:
            tts_status_var.set(str(exc))
            return

        def done(fut: "asyncio.Future") -> None:
            exc = fut.exception()
            if exc:
                root.after(0, tts_status_var.set, str(exc))
            else:
                root.after(
                    0,
                    lambda: (
                        tts_status_var.set(
                            "즉시 TTS 재생 요청이 전송되었습니다. 장치에서 재생을 준비합니다."
                        ),
                        tts_text.delete("1.0", tk.END),
                    ),
                )

        future.add_done_callback(done)

    ttk.Button(tts_button_frame, text="즉시 재생", command=send_tts).grid(
        row=0, column=0, sticky="e"
    )
    ttk.Label(
        tts_frame,
        textvariable=tts_status_var,
        foreground="#444",
        wraplength=860,
    ).grid(row=2, column=0, sticky="ew", pady=(6, 0))

    log_frame = ttk.LabelFrame(main_frame, text="이벤트 로그", padding=8)
    log_frame.grid(row=4, column=0, sticky="nsew", pady=(12, 0))
    log_frame.columnconfigure(0, weight=1)
    log_frame.rowconfigure(0, weight=1)

    log_text = tk.Text(log_frame, height=10, wrap="word", state="disabled")
    log_text.grid(row=0, column=0, sticky="nsew")

    log_scroll = ttk.Scrollbar(log_frame, orient="vertical", command=log_text.yview)
    log_text.configure(yscrollcommand=log_scroll.set)
    log_scroll.grid(row=0, column=1, sticky="ns")

    def update_schedule_list(new_schedules: List[Dict[str, Any]]) -> None:
        nonlocal schedules, selected_schedule_id

        def sync() -> None:
            nonlocal schedules, selected_schedule_id
            previous_id = selected_schedule_id
            schedules = list(new_schedules)
            available_ids = {
                sid for sid in (get_schedule_id(s) for s in schedules) if sid is not None
            }
            for sid in list(schedule_statuses.keys()):
                if sid not in available_ids:
                    schedule_statuses.pop(sid, None)

            schedule_listbox.delete(0, tk.END)
            for sch in schedules:
                schedule_listbox.insert(tk.END, format_schedule_item(sch))

            schedule_count_var.set(f"총 {len(schedules)}건의 예약")

            if not schedules:
                selected_schedule_id = None
                render_schedule_detail(None)
                return

            if previous_id is not None:
                for idx, sch in enumerate(schedules):
                    if get_schedule_id(sch) == previous_id:
                        schedule_listbox.selection_clear(0, tk.END)
                        schedule_listbox.selection_set(idx)
                        schedule_listbox.see(idx)
                        selected_schedule_id = previous_id
                        render_schedule_detail(schedules[idx])
                        break
                else:
                    schedule_listbox.selection_clear(0, tk.END)
                    schedule_listbox.selection_set(0)
                    selected_schedule_id = get_schedule_id(schedules[0])
                    render_schedule_detail(schedules[0])
            else:
                schedule_listbox.selection_clear(0, tk.END)
                schedule_listbox.selection_set(0)
                selected_schedule_id = get_schedule_id(schedules[0])
                render_schedule_detail(schedules[0])

        root.after(0, sync)

    def handle_schedule_error(message: str) -> None:
        root.after(0, operation_var.set, message)

    def handle_test_broadcast(
        schedule_id: Optional[int], payload: Optional[Dict[str, Any]]
    ) -> None:
        def sync() -> None:
            timestamp = time.strftime("%H:%M:%S")
            if schedule_id is None:
                operation_var.set("테스트 방송 명령을 수신했지만 schedule_id 정보가 없습니다.")
                return
            set_schedule_status(
                schedule_id,
                f"{timestamp} 테스트 방송 명령을 수신했습니다. 전송이 완료되었습니다.",
            )
            preview = ""
            if payload:
                preview = json.dumps(payload, ensure_ascii=False)
                if len(preview) > 200:
                    preview = preview[:197] + "…"
            operation_var.set(f"테스트 방송 명령 수신 (ID: {schedule_id})")
            if preview:
                append_log(f"테스트 방송 페이로드: {preview}", logging.DEBUG)

        root.after(0, sync)

    def handle_remote_tts(text: str) -> None:
        preview = text if len(text) < 40 else text[:37] + "…"
        root.after(
            0,
            lambda: (
                operation_var.set(
                    f"서버에서 즉시 TTS 재생 명령을 수신했습니다: '{preview}'"
                ),
                tts_status_var.set("서버에서 전달한 TTS가 곧 재생됩니다."),
            ),
        )

    def log_callback(message: str, level: int = logging.INFO) -> None:
        root.after(0, append_log, message, level)

    def update_status(text: str) -> None:
        root.after(0, status_var.set, text)

    def on_select(event: Optional[tk.Event] = None) -> None:  # noqa: ARG001
        nonlocal selected_schedule_id
        selection = schedule_listbox.curselection()
        if not selection:
            selected_schedule_id = None
            render_schedule_detail(None)
            return
        idx = selection[0]
        if idx >= len(schedules):
            return
        schedule = schedules[idx]
        selected_schedule_id = get_schedule_id(schedule)
        render_schedule_detail(schedule)

    schedule_listbox.bind("<<ListboxSelect>>", on_select)

    client = WSClient(
        update_status,
        on_schedules_updated=update_schedule_list,
        on_schedules_error=handle_schedule_error,
        on_test_broadcast=handle_test_broadcast,
        on_remote_tts=handle_remote_tts,
        log_callback=log_callback,
    )

    def create_image():
        image = Image.new("RGB", (64, 64), "white")
        d = ImageDraw.Draw(image)
        d.rectangle((16, 16, 48, 48), fill="black")
        return image

    def show_window():
        root.after(0, root.deiconify)

    def hide_window():
        root.after(0, root.withdraw)

    def toggle_window(icon, item):  # noqa: ARG001
        if root.state() == "withdrawn":
            show_window()
        else:
            hide_window()

    icon = None

    def on_close() -> None:
        operation_var.set("프로그램을 종료하는 중입니다…")
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

        icon = pystray.Icon("ws_client", create_image(), "Broadcast Client", menu=tray_menu)

    client.start()

    if icon:
        threading.Thread(target=icon.run, daemon=True).start()

    root.after(1000, refresh_schedules)
    root.protocol("WM_DELETE_WINDOW", on_close)
    root.mainloop()


if __name__ == "__main__":
    main()
