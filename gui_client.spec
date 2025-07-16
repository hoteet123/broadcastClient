# -*- mode: python ; coding: utf-8 -*-

block_cipher = None

# 1) 분석 단계 ─ 포함할 파이썬 스크립트 나열
a = Analysis(
    ['gui_client.py', 'vlc_embed.py', 'vlc_playlist.py', 'scheduler.py', 'display_config.py', 'media_cache.py'],
    pathex=['.'],          # 프로젝트 루트(필요하면 절대경로로 수정)
    binaries=[],           # 추가 DLL/EXE가 있으면 [('src', 'dest')] 형식으로 지정
    datas=[],              # 리소스 파일 있으면 [('src/*', 'dest')] 식으로 지정
    hiddenimports=[],      # 동적 import 모듈은 여기에
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],           # 제외할 모듈
    noarchive=False,
    optimize=0,
)

# 2) 파이썬 코드 → 압축된 pyz
pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

# 3) 실행 파일 생성 ─ onefile=True 가 핵심
exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name='gui_client',           # dist/gui_client.exe
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,                    # UPX 압축 (미설치 시 False 로)
    console=True,                # GUI 앱이면 False 로 바꿔도 됨
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=None,                   # 아이콘 넣으려면 'icon.ico'
    onefile=True                 # ★ 단일 EXE 플래그
)
