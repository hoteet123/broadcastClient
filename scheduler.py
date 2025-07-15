import asyncio
import datetime as dt
import json
import os
import pathlib
import subprocess
import sys
import tempfile
import threading
from typing import Any, Dict, List, Optional

import httpx

# When packaged as a single executable on Windows, ``__file__`` points to a
# temporary extraction directory. Use ``sys.argv[0]`` so ``client.cfg`` is
# loaded from the executable's directory.
if getattr(sys, "frozen", False) and sys.platform == "win32":
    CFG_PATH = pathlib.Path(sys.argv[0]).with_name("client.cfg")
else:
    CFG_PATH = pathlib.Path(__file__).with_name("client.cfg")


def load_config() -> Dict[str, Any]:
    if not CFG_PATH.exists():
        sample = {
            "HOST": "http://example.com:65000",
            "API_KEY": "",
            "DEVICE_ID": "PC-CLIENT",
        }
        CFG_PATH.write_text(json.dumps(sample, indent=2), encoding="utf-8")
        print(f"Created {CFG_PATH}. Fill in API_KEY and run again.")
        sys.exit(1)
    with CFG_PATH.open("r", encoding="utf-8") as f:
        return json.load(f)


def _cleanup_process(proc: subprocess.Popen, path: str) -> None:
    """Wait for the process to finish then delete the temporary file."""
    proc.wait()
    try:
        os.unlink(path)
    except FileNotFoundError:
        pass


def _play_mp3_path(path: str) -> None:
    """Play an MP3 file located at ``path`` and remove it afterwards."""
    if sys.platform.startswith("win"):
        def play_and_cleanup(p: str) -> None:
            try:
                import ctypes
                alias = f"mp3_{os.getpid()}_{threading.get_ident()}"
                mci = ctypes.windll.winmm.mciSendStringW
                mci(f'open "{p}" type mpegvideo alias {alias}', None, 0, None)
                mci(f'play {alias} wait', None, 0, None)
                mci(f'close {alias}', None, 0, None)
            finally:
                try:
                    os.unlink(p)
                except FileNotFoundError:
                    pass

        threading.Thread(target=play_and_cleanup, args=(path,), daemon=True).start()
    else:
        command = ["mpg123", "-q", path]
        proc = subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        threading.Thread(target=_cleanup_process, args=(proc, path), daemon=True).start()


def play_mp3_file(path: str) -> None:
    """Play an MP3 file and delete it after playback completes."""
    _play_mp3_path(path)


def set_volume(level: int) -> None:
    """Attempt to set system output volume (0-100)."""
    try:
        level = max(0, min(100, int(level)))
        if sys.platform.startswith("win"):
            import ctypes
            vol = int(level * 0xFFFF / 100)
            ctypes.windll.winmm.waveOutSetVolume(0xFFFFFFFF, vol | (vol << 16))
        elif sys.platform == "darwin":
            vol = level * 10 / 100
            subprocess.call(["osascript", "-e", f"set volume output volume {vol}"])
        else:
            subprocess.call(["amixer", "sset", "Master", f"{level}%"])
    except Exception as e:  # noqa: BLE001
        print(f"Failed to set volume: {e}")


def play_mp3(data: bytes) -> None:
    """Play MP3 data using an available backend without blocking."""
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".mp3")
    tmp.write(data)
    tmp.flush()
    tmp.close()

    _play_mp3_path(tmp.name)


async def tts_request(text: str, *, speed: float = 1.0, pitch: float = 0.2) -> bytes:
    ZONOS_URL = "http://211.170.18.15:8080/tts?output=mp3"

    payload = {
        "text": text,
        "language": "ko",
        "emotion": "",
        "pitch_std": None,
        "speaking_rate": None,
    }
    async with httpx.AsyncClient(timeout=120) as cli:
        r = await cli.post(ZONOS_URL, json=payload)
        r.raise_for_status()
        return r.content


def parse_days(mask: int) -> List[int]:
    """Return a list of weekday numbers from mask. Monday=0"""
    days = []
    for i in range(7):
        if mask & (1 << i):
            days.append(i)
    return days


def compute_next_run(sch: Dict[str, Any], base: Optional[dt.datetime] = None) -> Optional[dt.datetime]:
    base = base or dt.datetime.now()
    time_part = dt.time.fromisoformat(sch["ScheduledTime"])
    if sch.get("ScheduledDate"):
        day = dt.date.fromisoformat(sch["ScheduledDate"])
        when = dt.datetime.combine(day, time_part)
        return when if when >= base else None
    days = parse_days(sch.get("DaysOfWeekMask", 0))
    if not days:
        days = list(range(7))
    for i in range(7):
        day = base.date() + dt.timedelta(days=i)
        if day.weekday() in days:
            when = dt.datetime.combine(day, time_part)
            if when >= base:
                return when
    return None


async def fetch_schedules(cfg: Dict[str, Any]) -> List[Dict[str, Any]]:
    base_url = cfg["HOST"].rstrip("/")
    headers = {"X-API-Key": cfg["API_KEY"]}
    async with httpx.AsyncClient(base_url=base_url, http2=True, timeout=5.0) as cli:
        r = await cli.get("/broadcast-schedules", headers=headers)
        r.raise_for_status()
        data = r.json()
    return data.get("schedules", [])


async def scheduler_loop(
    schedules: List[Dict[str, Any]], stop_event: Optional[threading.Event] = None
) -> None:
    """Run scheduled playback based on a list of schedule dictionaries."""
    for sch in schedules:
        sch["next_run"] = compute_next_run(sch)
        sch["last_run"] = None
    while True:
        if stop_event and stop_event.is_set():
            break
        now = dt.datetime.now()
        for sch in schedules:
            nr = sch.get("next_run")
            if nr and nr <= now:
                last = sch.get("last_run")
                if last and last.date() == now.date():
                    sch["next_run"] = compute_next_run(sch, now + dt.timedelta(seconds=1))
                    continue
                try:
                    print(
                        f"Playing schedule {sch.get('ScheduleID')}: {sch.get('Title')}"
                    )
                    audio = await tts_request(
                        sch.get("TTSContent", ""),
                        speed=sch.get("Speed", 1.0),
                        pitch=sch.get("Pitch", 1.0),
                    )
                    play_mp3(audio)
                    sch["last_run"] = now
                except Exception as e:  # noqa: BLE001
                    print(f"Failed to play schedule {sch.get('ScheduleID')}: {e}")
                finally:
                    sch["next_run"] = compute_next_run(
                        sch, now + dt.timedelta(seconds=1)
                    )
        await asyncio.sleep(1)


async def run_scheduler(stop_event: Optional[threading.Event] = None) -> None:
    cfg = load_config()
    schedules = await fetch_schedules(cfg)
    await scheduler_loop(schedules, stop_event)


def run(schedules: List[Dict[str, Any]], stop_event: threading.Event) -> None:
    """Blocking helper to run scheduler_loop in the current thread."""
    asyncio.run(scheduler_loop(schedules, stop_event))


if __name__ == "__main__":
    asyncio.run(run_scheduler())
