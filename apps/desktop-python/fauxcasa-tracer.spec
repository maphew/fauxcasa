# PyInstaller spec for the tracer bullet app (fauxcasa-ncv: M1 Windows
# packaging spike). A real distributable bundle so the Windows CI gates run
# on "the artifact users would get", not on source. Decided per the
# stack-decision tripwire (fauxcasa-6hf / docs/research/stack-balloons.md):
# PyInstaller-class, ~100-150 MB is expected and acceptable — §7 anchors
# austerity on resident memory + cold start, not installer megabytes.
#
# Build from the repo root:
#   uv run --with "PySide6-Essentials==6.11.1" --with "pyinstaller==6.20.0" \
#       pyinstaller --noconfirm --clean apps/desktop-python/fauxcasa-tracer.spec
#
# Decisions (see the synthesis in the §7-validation report):
#   * ONEDIR, not onefile: onefile re-extracts the whole bundle to a temp
#     dir on every launch (multi-second cold start + AV friction) — that
#     directly fails §7's cold-start anchor.
#   * console=True: the tracer prints READY/JSON to stdout, which the
#     headless CI gate (QT_QPA_PLATFORM=offscreen) reads. A shipping,
#     double-clickable GUI build flips this to console=False (and must be
#     smoke-tested via screenshot, not stdout).
#   * PySide6-Essentials (the build env, not this file) physically omits
#     WebEngine/QML/Qt3D/Charts/translations at the source — more robust
#     than excludes alone. The excludes below are belt-and-suspenders so a
#     transitive import can't silently re-bloat the bundle under a gate.
#   * KEEP imageformats/ (qjpeg — JPEG is a plugin; QImage returns isNull()
#     with no error if it's missing => a silently blank grid), platforms/
#     (qwindows + qoffscreen) and styles/ (qmodernwindowsstyle, 6.7+).
#     The official PySide6 PyInstaller hook auto-collects these + writes
#     qt.conf; do NOT hand-prune them.
#   * --noupx: PyInstaller already excludes Qt DLLs from UPX; UPX corrupts
#     multi-segment Qt6 DLLs, worsens AV flags, and adds launch-time
#     decompression (hurts cold start).

import os

# Paths are resolved relative to this spec file (SPECPATH =
# apps/desktop-python), so the build works regardless of the CWD
# pyinstaller runs from.
_here = SPECPATH
_repo = os.path.dirname(os.path.dirname(_here))

# apps/desktop-python/main.py imports its siblings (catalog/grid/thumbcache/viewer) as
# top-level modules after sys.path.insert(parent) — they are NOT a package,
# so PyInstaller's module graph needs them named explicitly.
_hidden = ["catalog", "grid", "thumbcache", "viewer", "picasa_db"]

# Qt modules the tracer never touches (QtCore/QtGui/QtWidgets only). Mostly
# no-ops under PySide6-Essentials, but they make the build self-documenting
# and CI-robust against a transitive pull-in.
_qt_excludes = [
    "PySide6.QtWebEngineCore", "PySide6.QtWebEngineWidgets",
    "PySide6.QtWebEngineQuick", "PySide6.QtWebChannel",
    "PySide6.QtWebSockets", "PySide6.QtQml", "PySide6.QtQuick",
    "PySide6.QtQuick3D", "PySide6.QtQuickWidgets",
    "PySide6.QtQuickControls2", "PySide6.QtMultimedia",
    "PySide6.QtMultimediaWidgets", "PySide6.QtSpatialAudio",
    "PySide6.Qt3DCore", "PySide6.Qt3DRender", "PySide6.Qt3DInput",
    "PySide6.Qt3DLogic", "PySide6.Qt3DAnimation", "PySide6.Qt3DExtras",
    "PySide6.QtCharts", "PySide6.QtDataVisualization", "PySide6.QtGraphs",
    "PySide6.QtPdf", "PySide6.QtPdfWidgets", "PySide6.QtSql",
    "PySide6.QtTest", "PySide6.QtNetwork", "PySide6.QtBluetooth",
    "PySide6.QtNfc", "PySide6.QtPositioning", "PySide6.QtLocation",
    "PySide6.QtSensors", "PySide6.QtSerialPort", "PySide6.QtSerialBus",
    "PySide6.QtDesigner", "PySide6.QtUiTools", "PySide6.QtHelp",
    "PySide6.QtScxml", "PySide6.QtRemoteObjects",
    "PySide6.QtTextToSpeech", "PySide6.QtNetworkAuth",
    "PySide6.QtSvg", "PySide6.QtSvgWidgets",
    "PySide6.QtOpenGL", "PySide6.QtOpenGLWidgets",
]
_other_excludes = ["PyQt5", "PyQt6", "PySide2",
                   "tkinter", "unittest", "test", "lib2to3"]

a = Analysis(
    [os.path.join(_here, "main.py")],
    pathex=[_here, os.path.join(_repo, "scripts")],  # siblings + picasa_db
    binaries=[],
    datas=[],
    hiddenimports=_hidden,
    hookspath=[],
    excludes=_qt_excludes + _other_excludes,
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz, a.scripts, [],
    exclude_binaries=True,
    name="fauxcasa-tracer",
    debug=False,
    strip=False,
    upx=False,
    console=True,  # CI/smoke build; flip to False for a shipping GUI build
)
coll = COLLECT(
    exe, a.binaries, a.datas,
    strip=False, upx=False,
    name="fauxcasa-tracer",
)
