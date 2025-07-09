#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
EnterCRM API & WebSocket *Python Client*
────────────────────────────────────────
• 서버 WebSocket(/ws) → echo·ping 수신
• WS 연결 성공 후 HTTP /ping 및 /broadcast-schedules 호출
• 설정은 실행 경로의 client.cfg(JSON)에서 불러옴
• 비동기 실행:  python3 client.py

필수 패키지
    pip install "httpx[http2]" websockets
"""

from __future__ import annotations

import asyncio
import json
import pathlib
import secrets
import sys
from typing import Dict

import httpx
import websockets
from websockets.exceptions import ConnectionClosedOK

###############################################################################
# 1. 설정 로딩 (client.cfg)
###############################################################################

CFG_PATH = pathlib.Path(__file__).with_name("client.cfg")


def load_config() -> Dict[str, str]:
    """client.cfg 없으면 샘플 파일 생성 후 종료."""
    if CFG_PATH.exists():
        with CFG_PATH.open("r", encoding="utf-8") as f:
            return json.load(f)

    # 템플릿 생성
    sample = {
        "HOST": "http://211.170.18.15:65000",
        "API_KEY": "",          # 반드시 입력
        "DEVICE_ID": "PC-CLIENT",
        # 필요하면 다중 클라이언트 구분을 위해 랜덤 키 팁:
        # "DEVICE_ID": f"PC-{secrets.token_hex(2).upper()}",
    }
    CFG_PATH.write_text(json.dumps(sample, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"📝 {CFG_PATH.name} 파일을 생성했습니다. API_KEY를 채운 뒤 다시 실행하세요.")
    sys.exit(1)


cfg = load_config()
HOST: str = cfg["HOST"].rstrip("/")  # 끝 슬래시 제거
API_KEY: str = cfg["API_KEY"]
DEVICE_ID: str = cfg["DEVICE_ID"]

if not API_KEY:
    print("❌  config 파일에 API_KEY가 비어 있습니다.")
    sys.exit(1)

###############################################################################
# 2. WebSocket ↔ HTTP 데모
###############################################################################


async def websocket_demo() -> None:
    """WS 연결 후 echo·ping 수신, 이후 HTTP /ping 및 /broadcast-schedules 호출."""
    ws_url = (
        HOST.replace("http", "ws")
        + f"/ws?api_key={API_KEY}&device_id={DEVICE_ID}"
    )

    async with websockets.connect(
        ws_url,
        ping_interval=None,      # 서버 ping 브로드캐스트만 사용
        max_size=1 << 20,        # 1 MiB
    ) as ws:
        print("[WS] connected")

        # 1) 예시 메시지 전송
        await ws.send(json.dumps({"hello": "world"}))
        print("[WS] → {'hello': 'world'}")

        # 2) 수신 루프를 별도 태스크로 실행
        async def recv_loop():
            try:
                while True:
                    raw = await ws.recv()
                    print("[WS] ←", raw)
            except ConnectionClosedOK:
                print("[WS] 정상 종료")
            except Exception as e:
                print("[WS] 수신 오류:", e)

        recv_task = asyncio.create_task(recv_loop())

        # 3) WebSocket이 열린 상태에서 /ping 호출
        async with httpx.AsyncClient(base_url=HOST, http2=True, timeout=5.0) as cli:
            r = await cli.get("/ping", headers={"X-API-Key": API_KEY})
            r.raise_for_status()
            print("[HTTP] /ping →", r.json())

            # 서버와의 연결에 성공하면 방송 예약 목록을 가져온다
            r = await cli.get("/broadcast-schedules", headers={"X-API-Key": API_KEY})
            r.raise_for_status()
            print("[HTTP] /broadcast-schedules →", r.json())

        # 4) 5초 대기 후 WS 종료
        await asyncio.sleep(5)
        await ws.close()
        await recv_task


###############################################################################
# 3. 엔트리포인트
###############################################################################

async def main():
    try:
        await websocket_demo()
    except Exception as e:
        print("❌  Error:", e)


if __name__ == "__main__":
    asyncio.run(main())
