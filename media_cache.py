import sys
import pathlib
import hashlib
import time
from urllib.parse import urlparse
import httpx

# Directory to store cached media next to the executable
RUN_DIR = pathlib.Path(sys.argv[0]).resolve().parent
CACHE_DIR = RUN_DIR / "cache"


def download_media(url: str, progress_cb=None) -> str:
    """Download ``url`` to cache and return local path.

    ``progress_cb`` is called with ``(downloaded, total, speed, elapsed)``.
    The function blocks until the download is finished.
    """
    parsed = urlparse(url)
    if parsed.scheme in {"file", ""}:
        return url

    CACHE_DIR.mkdir(exist_ok=True)
    ext = pathlib.Path(parsed.path).suffix or ".bin"
    name = hashlib.sha1(url.encode()).hexdigest() + ext
    path = CACHE_DIR / name
    if path.exists():
        return str(path)

    tmp_path = path.with_suffix(path.suffix + ".part")
    if tmp_path.exists():
        tmp_path.unlink()

    start = time.monotonic()
    downloaded = 0
    total = 0
    with httpx.Client(timeout=None) as cli:
        with cli.stream("GET", url) as r:
            r.raise_for_status()
            total = int(r.headers.get("Content-Length") or 0)
            with open(tmp_path, "wb") as f:
                for chunk in r.iter_bytes(65536):
                    if not chunk:
                        continue
                    f.write(chunk)
                    downloaded += len(chunk)
                    elapsed = time.monotonic() - start
                    speed = downloaded / elapsed if elapsed > 0 else 0.0
                    if progress_cb:
                        progress_cb(downloaded, total, speed, elapsed)
    tmp_path.rename(path)
    if progress_cb:
        progress_cb(total, total, None, time.monotonic() - start)
    return str(path)
