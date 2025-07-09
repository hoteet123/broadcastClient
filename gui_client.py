import asyncio
import json
import pathlib
import threading
import sys
import tkinter as tk

from PIL import Image, ImageDraw
import pystray

import websockets
from websockets.exceptions import ConnectionClosed

CFG_PATH = pathlib.Path(__file__).with_name("client.cfg")


def load_config():
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


cfg = load_config()
HOST = cfg["HOST"].rstrip("/")
API_KEY = cfg["API_KEY"]
DEVICE_ID = cfg["DEVICE_ID"]


class WSClient:
    def __init__(self, update_status):
        self.update_status = update_status
        self.stop_event = threading.Event()
        self.thread = threading.Thread(target=self.run, daemon=True)

    def start(self):
        self.thread.start()

    def stop(self):
        self.stop_event.set()

    def run(self):
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        loop.run_until_complete(self.connect_loop())

    async def connect_loop(self):
        ws_url = HOST.replace("http", "ws") + f"/ws?api_key={API_KEY}&device_id={DEVICE_ID}"
        backoff = 1
        while not self.stop_event.is_set():
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

    async def handle_ws(self, ws):
        try:
            await ws.send(json.dumps({"hello": "world"}))
            while not self.stop_event.is_set():
                await ws.recv()
        except ConnectionClosed:
            pass


def create_image() -> Image.Image:
    """Create a simple tray icon image."""
    image = Image.new("RGB", (64, 64), "white")
    draw = ImageDraw.Draw(image)
    draw.rectangle((16, 16, 48, 48), fill="black")
    return image


def main():
    root = tk.Tk()
    root.title("WS Client")
    status_var = tk.StringVar(value="Starting")
    tk.Label(root, textvariable=status_var, width=40).pack(padx=20, pady=20)

    def update_status(text):
        root.after(0, status_var.set, text)

    client = WSClient(update_status)
    client.start()

    tray_icon = None
    visible = True

    def toggle(icon, item):
        nonlocal visible
        if visible:
            root.withdraw()
            visible = False
        else:
            root.deiconify()
            root.focus_force()
            visible = True
        icon.update_menu()

    def item_title():
        return "Hide" if visible else "Show"

    tray_icon = pystray.Icon(
        "ws-client",
        create_image(),
        "WS Client",
        menu=pystray.Menu(pystray.MenuItem(item_title, toggle))
    )
    threading.Thread(target=tray_icon.run, daemon=True).start()

    def on_close():
        client.stop()
        tray_icon.stop()
        root.destroy()

    root.protocol("WM_DELETE_WINDOW", on_close)
    root.mainloop()


if __name__ == "__main__":
    main()
