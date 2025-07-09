import asyncio
import datetime as dt
import json
import os
import pathlib
import subprocess
import sys
import tempfile
import time
from typing import Any, Dict, List, Optional

import httpx

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


def play_mp3(data: bytes) -> None:
    """Play MP3 data using an available backend."""
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".mp3")
    try:
        tmp.write(data)
        tmp.flush()
        tmp.close()
        if sys.platform.startswith("win"):
            subprocess.run([
                "powershell",
                "-NoProfile",
                "-Command",
                f"Start-Process -FilePath '{tmp.name}' -Wait"
            ], check=True)
        else:
            subprocess.run(["mpg123", "-q", tmp.name], check=True)
    finally:
        os.unlink(tmp.name)


async def tts_request(text: str, *, speed: float = 1.0, pitch: float = 1.0) -> bytes:
    ZONOS_URL = "http://211.170.18.15:8080/tts?output=mp3"
    payload = {
        "text": text,
        "language": "ko",
        "emotion": None,
        "pitch_std": pitch,
        "speaking_rate": speed,
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


async def run_scheduler() -> None:
    cfg = load_config()
    schedules = await fetch_schedules(cfg)
    for sch in schedules:
        sch["next_run"] = compute_next_run(sch)
    while True:
        now = dt.datetime.now()
        for sch in schedules:
            nr = sch.get("next_run")
            if nr and nr <= now:
                print(f"Playing schedule {sch.get('ScheduleID')}: {sch.get('Title')}")
                audio = await tts_request(sch.get("TTSContent", ""), speed=sch.get("Speed", 1.0), pitch=sch.get("Pitch", 1.0))
                play_mp3(audio)
                sch["next_run"] = compute_next_run(sch, now + dt.timedelta(seconds=1))
        await asyncio.sleep(1)


if __name__ == "__main__":
    asyncio.run(run_scheduler())
