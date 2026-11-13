# PyInstaller recipe: one double-clickable app with Python, numpy/scipy and PortAudio inside.
# Build from the repo root:  pyinstaller packaging/ghostvoice.spec --noconfirm
import os
import sys

sys.path.insert(0, os.path.dirname(SPECPATH))
from ghostvoice import __version__  # noqa: E402

block_cipher = None
excludes = ["matplotlib", "pytest", "IPython", "PIL", "pystoi", "pandas"]  # never needed; keep scipy whole

a = Analysis(
    [os.path.join(SPECPATH, "ghostvoice_app.py")],
    pathex=[os.path.dirname(SPECPATH)],  # the repo root, so the package is found even with `pip install -e`
    excludes=excludes,
    noarchive=False,
)
pyz = PYZ(a.pure, cipher=block_cipher)

if sys.platform == "darwin":
    # macOS: a normal .app bundle (one-file apps are deprecated there).
    exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name="GhostVoice", console=False, upx=False)
    coll = COLLECT(exe, a.binaries, a.datas, name="GhostVoice", upx=False)
    app = BUNDLE(
        coll,
        name="GhostVoice.app",
        bundle_identifier="io.github.ghostvoice",
        info_plist={
            "CFBundleShortVersionString": __version__,
            "NSMicrophoneUsageDescription": "GhostVoice filters your microphone live to give your voice a ghostly swell.",
            "NSHighResolutionCapable": True,
        },
    )
else:
    # Windows / Linux: a single file you can double-click.
    exe = EXE(
        pyz, a.scripts, a.binaries, a.datas, [],
        name="GhostVoice",
        console=False,
        upx=False,
    )
