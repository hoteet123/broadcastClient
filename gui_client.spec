# -*- mode: python ; coding: utf-8 -*-


a = Analysis(
    ['gui_client.py'],
    pathex=[],
    binaries=[],
    datas=[],
    hiddenimports=[
    'PIL', 'httpx', 'httpcore', 'h11',
    'anyio', 'sniffio', 'idna', 'certifi',
    'pystray',                 # ★
    'pystray._util', 'pystray._base', 'pystray._win32',  # 서브모듈
    'win32api', 'win32gui', 'win32con', 'pywintypes',    # pywin32
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='gui_client',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name='gui_client',
)
