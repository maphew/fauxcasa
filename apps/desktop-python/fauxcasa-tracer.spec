# PyInstaller spec for the tracer bullet app (fauxcasa-ncv: M1 Windows
# packaging spike). A real distributable bundle so the Windows CI gates run
# on "the artifact users would get", not on source. Decided per the
# stack-decision tripwire (fauxcasa-6hf / docs/research/stack-balloons.md):
# PyInstaller-class, ~100-150 MB is expected and acceptable — §7 anchors
# austerity on resident memory + cold start, not installer megabytes.
#
# Build from the repo root (ONE invocation produces BOTH variants below;
# rawpy AND av MUST be in the build env or the artifact silently ships
# without RAW/video decode — rawload.py and videoload.py import them
# lazily, so nothing fails at build):
#   uv run --with "PySide6-Essentials==6.11.1" --with "pyinstaller==6.20.0" \
#       --with "rawpy==0.27.0" --with "av==18.0.0" \
#       pyinstaller --noconfirm --clean apps/desktop-python/fauxcasa-tracer.spec
#
# Decisions (see the synthesis in the §7-validation report):
#   * ONEDIR, not onefile: onefile re-extracts the whole bundle to a temp
#     dir on every launch (multi-second cold start + AV friction) — that
#     directly fails §7's cold-start anchor.
#   * TWO variants from one Analysis (fauxcasa-pqw) — one analysis/PYZ, an
#     extra cheap COLLECT copy, no second pyinstaller run:
#       - dist/fauxcasa-tracer/      console=True  — the CI/smoke build. It
#         prints READY/JSON to stdout (the offscreen headless gate parses it)
#         and its stderr carries the diagnostics bundle.yml greps.
#       - dist/fauxcasa-tracer-gui/  console=False — the shipping, double-
#         clickable build: no black console window. A windowed PyInstaller
#         process has sys.stdout/sys.stderr == None, so the §7 stdout prints
#         become harmless no-ops and the human diagnostics + Qt messages +
#         uncaught tracebacks go to the rotating log file via applog instead.
#         Smoke-tested by screenshot + asserting that log file, never stdout.
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
# so PyInstaller's module graph needs them named explicitly. "exiv2" (the
# python-exiv2 binding behind metareader), "rawpy" (the LibRaw wheel
# behind the rawload seam, fauxcasa-v46.1) and "av" (the PyAV/libav wheel
# behind the videoload poster seam, fauxcasa-v46.2) are imported lazily
# inside functions with fail-soft fallbacks that would otherwise let a
# missed collection ship a build that silently reads no dates/GPS/Rating,
# error-tiles every RAW, or error-tiles every video poster — named here
# so the graph can never drop them. Wheels only, no system libs.
_hidden = ["catalog", "grid", "thumbcache", "viewer", "slideshow", "peek",
           "picasa_db", "applog", "metareader", "exiv2", "rawload", "rawpy",
           "videoload", "av"]

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

# Two EXEs off the one Analysis/PYZ, differing ONLY in the console flag and
# name. The shipping windowed build (console=False) suppresses the console
# window; diagnostics survive via applog's log file (see the header note).
_exe_common = dict(
    exclude_binaries=True,
    debug=False,
    strip=False,
    upx=False,
)

exe_console = EXE(
    pyz, a.scripts, [],
    name="fauxcasa-tracer",
    console=True,            # CI/smoke: READY/JSON on stdout, diagnostics on stderr
    **_exe_common,
)
exe_gui = EXE(
    pyz, a.scripts, [],
    name="fauxcasa-tracer-gui",
    console=False,           # shipping double-click: no console window
    **_exe_common,
)

# One COLLECT (onedir tree) per variant. a.binaries/a.datas are shared, so
# the second collect is just a copy — the expensive Analysis ran once.
coll_console = COLLECT(
    exe_console, a.binaries, a.datas,
    strip=False, upx=False,
    name="fauxcasa-tracer",
)
coll_gui = COLLECT(
    exe_gui, a.binaries, a.datas,
    strip=False, upx=False,
    name="fauxcasa-tracer-gui",
)
