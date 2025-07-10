# Broadcast Client

A simple GUI/WebSocket client to fetch broadcast schedules and play TTS audio.

## Setup

Install dependencies (requires Python 3.8+):

```bash
pip install websockets "httpx[http2]" pillow pystray
```

`httpx` is used with HTTP/2 enabled. If the optional `h2` package is not installed
(i.e. if you only run `pip install httpx`), the program will fail with an error
similar to:

```
ImportError: Using http2=True, but the 'h2' package is not installed.
```

Ensure you install `httpx` with the `[http2]` extra to avoid connection loops on
systems where `h2` is missing.

## Running

Edit `client.cfg` with your `HOST`, `API_KEY` and `DEVICE_ID`, then run:

```bash
python gui_client.py
```

or just the scheduler:

```bash
python scheduler.py
```
