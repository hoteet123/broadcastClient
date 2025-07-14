import sys
import subprocess
import re
import ctypes


class _DEVMODE(ctypes.Structure):
    _fields_ = [
        ("dmDeviceName", ctypes.c_wchar * 32),
        ("dmSpecVersion", ctypes.c_uint16),
        ("dmDriverVersion", ctypes.c_uint16),
        ("dmSize", ctypes.c_uint16),
        ("dmDriverExtra", ctypes.c_uint16),
        ("dmFields", ctypes.c_uint32),
        ("dmOrientation", ctypes.c_int16),
        ("dmPaperSize", ctypes.c_int16),
        ("dmPaperLength", ctypes.c_int16),
        ("dmPaperWidth", ctypes.c_int16),
        ("dmScale", ctypes.c_int16),
        ("dmCopies", ctypes.c_int16),
        ("dmDefaultSource", ctypes.c_int16),
        ("dmPrintQuality", ctypes.c_int16),
        ("dmColor", ctypes.c_int16),
        ("dmDuplex", ctypes.c_int16),
        ("dmYResolution", ctypes.c_int16),
        ("dmTTOption", ctypes.c_int16),
        ("dmCollate", ctypes.c_int16),
        ("dmFormName", ctypes.c_wchar * 32),
        ("dmLogPixels", ctypes.c_uint16),
        ("dmBitsPerPel", ctypes.c_uint32),
        ("dmPelsWidth", ctypes.c_uint32),
        ("dmPelsHeight", ctypes.c_uint32),
        ("dmDisplayFlags", ctypes.c_uint32),
        ("dmDisplayFrequency", ctypes.c_uint32),
        ("dmICMMethod", ctypes.c_uint32),
        ("dmICMIntent", ctypes.c_uint32),
        ("dmMediaType", ctypes.c_uint32),
        ("dmDitherType", ctypes.c_uint32),
        ("dmReserved1", ctypes.c_uint32),
        ("dmReserved2", ctypes.c_uint32),
        ("dmPanningWidth", ctypes.c_uint32),
        ("dmPanningHeight", ctypes.c_uint32),
        ("dmDisplayOrientation", ctypes.c_uint32),
    ]


DM_PELSWIDTH = 0x80000
DM_PELSHEIGHT = 0x100000
DM_DISPLAYORIENTATION = 0x80

DMDO_DEFAULT = 0
DMDO_90 = 1
DMDO_180 = 2
DMDO_270 = 3


_orientation_map_linux = {
    0: "normal",
    1: "left",
    2: "inverted",
    3: "right",
}


def _parse_resolution(res: str):
    match = re.match(r"^(\d+)x(\d+)$", res or "")
    if match:
        return int(match.group(1)), int(match.group(2))
    return None


def _windows_apply(resolution, orientation):
    user32 = ctypes.windll.user32
    dm = _DEVMODE()
    dm.dmSize = ctypes.sizeof(_DEVMODE)
    ENUM_CURRENT_SETTINGS = -1
    if not user32.EnumDisplaySettingsW(None, ENUM_CURRENT_SETTINGS, ctypes.byref(dm)):
        return
    changed = False
    if resolution:
        width, height = resolution
        if dm.dmPelsWidth != width or dm.dmPelsHeight != height:
            dm.dmPelsWidth = width
            dm.dmPelsHeight = height
            dm.dmFields |= DM_PELSWIDTH | DM_PELSHEIGHT
            changed = True
    if orientation is not None and orientation in {DMDO_DEFAULT, DMDO_90, DMDO_180, DMDO_270}:
        if dm.dmDisplayOrientation != orientation:
            dm.dmDisplayOrientation = orientation
            dm.dmFields |= DM_DISPLAYORIENTATION
            if orientation in {DMDO_90, DMDO_270}:
                dm.dmPelsWidth, dm.dmPelsHeight = dm.dmPelsHeight, dm.dmPelsWidth
                dm.dmFields |= DM_PELSWIDTH | DM_PELSHEIGHT
            changed = True
    if changed:
        CDS_UPDATEREGISTRY = 1
        user32.ChangeDisplaySettingsW(ctypes.byref(dm), CDS_UPDATEREGISTRY)


def _linux_apply(resolution, orientation):
    try:
        xrandr = subprocess.check_output(["xrandr", "--query"], text=True)
    except Exception:
        return
    output = None
    for line in xrandr.splitlines():
        if " connected" in line:
            output = line.split()[0]
            if " primary" in line:
                break
    if not output:
        return
    cmd = ["xrandr", "--output", output]
    if resolution:
        cmd += ["--mode", f"{resolution[0]}x{resolution[1]}"]
    if orientation in _orientation_map_linux:
        cmd += ["--rotate", _orientation_map_linux[orientation]]
    subprocess.call(cmd)


def apply_display_settings(resolution_str=None, orientation=None):
    resolution = _parse_resolution(resolution_str) if resolution_str else None
    if sys.platform.startswith("win"):
        _windows_apply(resolution, orientation)
    else:
        _linux_apply(resolution, orientation)
