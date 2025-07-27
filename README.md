# Broadcast Client

A simple GUI/WebSocket client to fetch broadcast schedules and play TTS audio.

## Setup

Install dependencies (requires Python 3.8+):

```bash
pip install websockets "httpx[http2]" pillow pystray python-vlc
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
python enterplayer.py
```

The client stores the machine's MAC address in `client.cfg`. On the first run
the address is detected and saved under `MAC_ADDRESS`. This value is reused on
subsequent runs so each instance can be consistently identified even if the
hardware's MAC address changes across reboots.

or just the scheduler:

```bash
python scheduler.py
```

When the server sends a config message with `Playmode` set to `1`,
`gui_client.py` will launch `vlc_embed.py` to play a provided `StreamURL`
in a fullscreen embedded VLC window. The helper script attaches VLC to a
Tkinter window using the correct API for Windows, macOS, or X11-based
Linux/Raspbian environments.
If no stream URL is supplied, it defaults to `http://nas.3no.kr/test.mp4`.

Config messages may also include `Resolution` (e.g. `"1920x1080"`) and
`Orientation` (0-4) fields.  When present, the client attempts to update the
system's display settings accordingly on Windows and Linux (including
Raspberry Pi and Orange Pi) using platform specific commands.
Additionally a `GuiImages` array may be provided to overlay elements on top of
the VLC window.  Each entry should contain `ImageUrl`, `X`, `Y`, `Width`,
`Height`, `GuiKind`, `GuiOrder` and `Monitor` values.  `GuiKind` can be
`image`, `video` or `url` and determines whether an image, video or embedded
web view is shown on the specified monitor.  `GuiOrder` starts at `0` and lower
numbers are placed above higher values, allowing precise control of the
stacking order.  GIF images animate and transparency is respected.

For `url` overlays on Windows the helper scripts try to launch Microsoft Edge
or Chrome in app mode for better performance. If no supported browser is found,
they fall back to the built in tkinter web view.

The client also handles playlist messages. When a playlist is received it
is passed to `vlc_playlist.py` for fullscreen playback. A subsequent
`play-media` command with a `media_id` will immediately start playback of
the matching playlist item.

`play-tts` messages can also be sent to immediately read arbitrary text
using the same TTS backend used for scheduled broadcasts.
