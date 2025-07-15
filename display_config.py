import sys
import subprocess
import ctypes
from typing import Optional, Union


def set_display_config(resolution: Optional[str] = None, orientation: Optional[Union[int, str]] = None) -> None:
    """Set display resolution and orientation if possible."""
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
            _set_windows_display(width, height, orientation)
        except Exception as e:
            print(f"Failed to set Windows display: {e}")
    else:
        try:
            _set_xrandr_display(width, height, orientation)
        except Exception as e:
            print(f"Failed to set xrandr display: {e}")


def _set_xrandr_display(width: Optional[int], height: Optional[int], orientation: Optional[int]) -> None:
    output = subprocess.run(['xrandr'], capture_output=True, text=True)
    if output.returncode != 0:
        return
    primary = None
    for line in output.stdout.splitlines():
        if ' connected primary' in line:
            primary = line.split()[0]
            break
    if not primary:
        for line in output.stdout.splitlines():
            if ' connected' in line:
                primary = line.split()[0]
                break
    if not primary:
        return
    cmd = ['xrandr', '--output', primary]
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

    def _set_windows_display(width: Optional[int], height: Optional[int], orientation: Optional[int]) -> None:
        user32 = ctypes.windll.user32
        dm = DEVMODE()
        dm.dmSize = ctypes.sizeof(DEVMODE)
        if user32.EnumDisplaySettingsW(None, ENUM_CURRENT_SETTINGS, ctypes.byref(dm)) == 0:
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
            user32.ChangeDisplaySettingsW(ctypes.byref(dm), CDS_UPDATEREGISTRY)
else:
    def _set_windows_display(width: Optional[int], height: Optional[int], orientation: Optional[int]) -> None:
        pass
