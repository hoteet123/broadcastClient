import sys
import subprocess
import ctypes
from typing import Optional, Union, List, Tuple, Dict

try:
    from screeninfo import get_monitors
except Exception:  # pragma: no cover - optional dependency may not be installed
    get_monitors = None


def _xrandr_outputs() -> List[str]:
    """Return connected display names with primary output first."""
    out = subprocess.run(["xrandr"], capture_output=True, text=True)
    if out.returncode != 0:
        return []
    primary = []
    others = []
    for line in out.stdout.splitlines():
        if " connected" in line:
            name = line.split()[0]
            if "primary" in line:
                primary.append(name)
            else:
                others.append(name)
    return primary + others


def _xrandr_monitor_geometries() -> List[Tuple[int, int, int, int]]:
    """Return monitor geometries via xrandr if available."""
    out = subprocess.run(["xrandr"], capture_output=True, text=True)
    if out.returncode != 0:
        return []
    geoms: Dict[str, Tuple[int, int, int, int]] = {}
    for line in out.stdout.splitlines():
        if " connected" not in line:
            continue
        parts = line.split()
        name = parts[0]
        geom = None
        for p in parts:
            if "x" in p and "+" in p and not p.startswith("("):
                geom = p
                break
        if geom is None:
            continue
        try:
            res, x, y = geom.split("+")[:3]
            w, h = res.split("x")
            geoms[name] = (int(x), int(y), int(w), int(h))
        except Exception:
            continue
    outputs = _xrandr_outputs()
    return [geoms.get(o, (0, 0, 0, 0)) for o in outputs if o in geoms]


def get_monitor_count() -> int:
    """Return the number of connected displays."""
    if sys.platform.startswith("win"):
        try:
            user32 = ctypes.windll.user32
            return max(1, int(user32.GetSystemMetrics(80)))
        except Exception:
            return 1
    else:
        outputs = _xrandr_outputs()
        return max(1, len(outputs))


def get_monitor_geometry(monitor: int = 1) -> Optional[Tuple[int, int, int, int]]:
    """Return ``(x, y, width, height)`` for ``monitor`` if available."""
    mons = []
    if get_monitors is not None:
        try:
            mons = get_monitors()
        except Exception:
            mons = []
    if not mons:
        if sys.platform.startswith("win"):
            mons = _win_monitor_geometries()
        else:
            mons = _xrandr_monitor_geometries()
    if not mons or monitor < 1 or monitor > len(mons):
        return None
    m = mons[monitor - 1]
    try:
        return int(m[0]), int(m[1]), int(m[2]), int(m[3])
    except Exception:
        try:
            return int(m.x), int(m.y), int(m.width), int(m.height)  # type: ignore[attr-defined]
        except Exception:
            return None


def apply_window_geometry(window, monitor: int = 1) -> None:
    """Move ``window`` to the specified monitor if geometry is available."""
    geom = get_monitor_geometry(monitor)
    if geom is None:
        return
    x, y, w, h = geom
    try:
        window.geometry(f"{w}x{h}+{x}+{y}")
    except Exception:
        pass
    if sys.platform.startswith("win"):
        try:
            hwnd = int(window.winfo_id())
            ctypes.windll.user32.MoveWindow(hwnd, x, y, w, h, True)
        except Exception:
            pass


def set_display_config(
    resolution: Optional[str] = None,
    orientation: Optional[Union[int, str]] = None,
    *,
    monitor: int = 1,
) -> None:
    """Set display resolution and orientation for the given ``monitor`` if possible."""
    width: Optional[int] = None
    height: Optional[int] = None
    if resolution:
        try:
            w, h = resolution.lower().split('x')
            width = int(w)
            height = int(h)
        except Exception:
            width = height = None
    if orientation is not None:
        try:
            orientation = int(orientation)
        except Exception:
            orientation = None
    if sys.platform.startswith('win'):
        try:
            _set_windows_display(width, height, orientation, monitor)
        except Exception as e:
            print(f"Failed to set Windows display: {e}")
    else:
        try:
            _set_xrandr_display(width, height, orientation, monitor)
        except Exception as e:
            print(f"Failed to set xrandr display: {e}")


def _set_xrandr_display(
    width: Optional[int],
    height: Optional[int],
    orientation: Optional[int],
    monitor: int,
) -> None:
    outputs = _xrandr_outputs()
    if not outputs or monitor < 1 or monitor > len(outputs):
        return
    name = outputs[monitor - 1]
    cmd = ['xrandr', '--output', name]
    if width and height:
        cmd += ['--mode', f'{width}x{height}']
    if orientation is not None:
        val = int(orientation)
        if val in {0, 1, 2, 3, 4}:
            angle = {0: 0, 1: 90, 2: 180, 3: 270, 4: 0}[val]
        else:
            angle = val % 360
        ori_map = {0: 'normal', 90: 'left', 180: 'inverted', 270: 'right'}
        cmd += ['--rotate', ori_map.get(angle, 'normal')]
    subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


if sys.platform.startswith('win'):
    from ctypes import wintypes

    class DEVMODE(ctypes.Structure):
        _fields_ = [
            ('dmDeviceName', wintypes.WCHAR * 32),
            ('dmSpecVersion', wintypes.WORD),
            ('dmDriverVersion', wintypes.WORD),
            ('dmSize', wintypes.WORD),
            ('dmDriverExtra', wintypes.WORD),
            ('dmFields', wintypes.DWORD),
            ('dmPositionX', wintypes.LONG),
            ('dmPositionY', wintypes.LONG),
            ('dmDisplayOrientation', wintypes.DWORD),
            ('dmDisplayFixedOutput', wintypes.DWORD),
            ('dmColor', wintypes.WORD),
            ('dmDuplex', wintypes.WORD),
            ('dmYResolution', wintypes.WORD),
            ('dmTTOption', wintypes.WORD),
            ('dmCollate', wintypes.WORD),
            ('dmFormName', wintypes.WCHAR * 32),
            ('dmLogPixels', wintypes.WORD),
            ('dmBitsPerPel', wintypes.DWORD),
            ('dmPelsWidth', wintypes.DWORD),
            ('dmPelsHeight', wintypes.DWORD),
            ('dmDisplayFlags', wintypes.DWORD),
            ('dmDisplayFrequency', wintypes.DWORD),
            ('dmICMMethod', wintypes.DWORD),
            ('dmICMIntent', wintypes.DWORD),
            ('dmMediaType', wintypes.DWORD),
            ('dmDitherType', wintypes.DWORD),
            ('dmReserved1', wintypes.DWORD),
            ('dmReserved2', wintypes.DWORD),
            ('dmPanningWidth', wintypes.DWORD),
            ('dmPanningHeight', wintypes.DWORD),
        ]

    ENUM_CURRENT_SETTINGS = -1
    CDS_UPDATEREGISTRY = 0x00000001
    DM_PELSWIDTH = 0x00080000
    DM_PELSHEIGHT = 0x00100000
    DM_DISPLAYORIENTATION = 0x00000080

    ORI_MAP = {
        0: 0,
        1: 1,
        2: 2,
        3: 3,
        4: 0,
        90: 1,
        180: 2,
        270: 3,
    }

    def _win_monitor_geometries() -> List[Tuple[int, int, int, int]]:
        geoms: List[Tuple[int, int, int, int]] = []
        try:
            user32 = ctypes.windll.user32
            count = int(user32.GetSystemMetrics(80))
        except Exception:
            return geoms
        for i in range(1, count + 1):
            try:
                dev_name = f"\\\\.\\DISPLAY{i}"
                dm = DEVMODE()
                dm.dmSize = ctypes.sizeof(DEVMODE)
                if user32.EnumDisplaySettingsW(dev_name, ENUM_CURRENT_SETTINGS, ctypes.byref(dm)) == 0:
                    continue
                geoms.append((int(dm.dmPositionX), int(dm.dmPositionY), int(dm.dmPelsWidth), int(dm.dmPelsHeight)))
            except Exception:
                continue
        return geoms

    def _set_windows_display(
        width: Optional[int],
        height: Optional[int],
        orientation: Optional[int],
        monitor: int,
    ) -> None:
        user32 = ctypes.windll.user32
        dm = DEVMODE()
        dm.dmSize = ctypes.sizeof(DEVMODE)
        dev_name = f"\\\\.\\DISPLAY{monitor}"
        if user32.EnumDisplaySettingsW(dev_name, ENUM_CURRENT_SETTINGS, ctypes.byref(dm)) == 0:
            return
        changed = False
        if orientation is not None:
            val = int(orientation)
            ori = ORI_MAP.get(val)
            if ori is None:
                ori = ORI_MAP.get(val % 360, 0)
            if dm.dmDisplayOrientation != ori:
                dm.dmDisplayOrientation = ori
                dm.dmFields |= DM_DISPLAYORIENTATION
                changed = True
            deg = {0: 0, 1: 90, 2: 180, 3: 270}.get(ori, 0)
        else:
            deg = {0: 0, 1: 90, 2: 180, 3: 270}.get(dm.dmDisplayOrientation, 0)
        if width is None or height is None:
            width = dm.dmPelsWidth
            height = dm.dmPelsHeight
            if deg in (90, 270):
                width, height = height, width
        if width and height:
            if dm.dmPelsWidth != width or dm.dmPelsHeight != height:
                dm.dmPelsWidth = width
                dm.dmPelsHeight = height
                dm.dmFields |= DM_PELSWIDTH | DM_PELSHEIGHT
                changed = True
        if changed:
            user32.ChangeDisplaySettingsExW(dev_name, ctypes.byref(dm), None, CDS_UPDATEREGISTRY, None)
else:
    def _set_windows_display(
        width: Optional[int],
        height: Optional[int],
        orientation: Optional[int],
        monitor: int,
    ) -> None:
        pass
