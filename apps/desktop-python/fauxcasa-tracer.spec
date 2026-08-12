# PyInstaller spec for the tracer bullet app (fauxcasa-ncv: M1 Windows
# packaging spike). A real distributable bundle so the Windows CI gates run
# on "the artifact users would get", not on source. Decided per the
# stack-decision tripwire (fauxcasa-6hf / docs/research/stack-balloons.md):
# PyInstaller-class, ~100-150 MB is expected and acceptable — §7 anchors
# austerity on resident memory + cold start, not installer megabytes.
#
# Build from the repo root (ONE invocation produces BOTH variants below;
# rawpy, av AND pillow MUST be in the build env or the artifact silently
# ships without RAW/video/PSD decode — rawload.py, videoload.py and
# pillowload.py import them lazily, so nothing fails at build. PySide6-
# Addons is needed ONLY for QtMultimedia's QAudioSink — Qt classes
# Multimedia as an add-on, so Essentials alone would silently ship a
# build whose video playback has no sound; the excludes + the binary
# filter below keep every OTHER Addons module out):
#   uv run --with "PySide6-Essentials==6.11.1" \
#       --with "PySide6-Addons==6.11.1" --with "pyinstaller==6.20.0" \
#       --with "rawpy==0.27.0" --with "av==18.0.0" --with "pillow==12.3.0" \
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
# behind the rawload seam, fauxcasa-v46.1), "av" (the PyAV/libav wheel
# behind the videoload poster seam, fauxcasa-v46.2) and "PIL" (the Pillow
# wheel behind the pillowload PSD/still fallback, fauxcasa-v46.4) are
# imported lazily inside functions with fail-soft fallbacks that would
# otherwise let a missed collection ship a build that silently reads no
# dates/GPS/Rating, error-tiles every RAW, error-tiles every video
# poster, or error-tiles every PSD — named here so the graph can never
# drop them. "zstandard" (fauxcasa-ed5.5) is the same story: catalog.py's
# save_catalog/load_catalog import it lazily inside the function bodies
# (so scripts that never persist a catalog don't need it), which would
# otherwise let a missed collection ship a build that silently fails to
# save/load catalog.json at all. Wheels only, no system libs.
_hidden = ["catalog", "grid", "thumbcache", "viewer", "slideshow", "peek",
           "inspector",
           "picasa_db", "applog", "metareader", "exiv2", "rawload", "rawpy",
           "videoload", "av", "pillowload", "PIL", "PIL.Image", "PIL.ImageOps",
           # Video playback (fauxcasa-v46.3): videostream is BOTH the broker
           # class viewer.py imports lazily AND the sandboxed worker
           # entrypoint; decodesvc carries its typed errors/StreamInfo. "av"
           # above (v46.2) is the worker's decoder — PyAV ships its OWN
           # libav wheels, untouched by the Qt-ffmpeg filter below.
           # Frozen worker spawn: videostream spawns [sys.executable,
           # "--worker", ...] when frozen (sys.executable is the app exe)
           # and main.py dispatches --worker to videostream._worker_main
           # BEFORE its argparse, so the bundle's playback worker is the
           # bundle itself — no separate launcher needed until
           # fauxcasa-i92.3's sandbox launcher supersedes it.
           # PySide6.QtMultimedia is imported lazily+guarded (viewer.
           # _make_audio_sink) for QAudioSink ONLY — see the i92.1 note at
           # the binary filter below for why that import is compliant; it
           # is ALSO in main.BUNDLE_RUNTIME_MODULES so --bundle-self-check
           # fails loudly on a build without PySide6-Addons.
           "videostream", "decodesvc", "PySide6.QtMultimedia",
           "zstandard"]

# Qt modules the tracer never touches (QtCore/QtGui/QtWidgets only). Mostly
# no-ops under PySide6-Essentials, but they make the build self-documenting
# and CI-robust against a transitive pull-in.
_qt_excludes = [
    "PySide6.QtWebEngineCore", "PySide6.QtWebEngineWidgets",
    "PySide6.QtWebEngineQuick", "PySide6.QtWebChannel",
    "PySide6.QtWebSockets", "PySide6.QtQml", "PySide6.QtQuick",
    "PySide6.QtQuick3D", "PySide6.QtQuickWidgets",
    # PySide6.QtMultimedia is deliberately NOT excluded anymore
    # (fauxcasa-v46.3): QAudioSink lives there and plays the PCM our
    # sandboxed video worker produces. Only the python module + the core
    # Qt6Multimedia library ship — the ffmpeg DECODE backend is stripped
    # by the binary filter below, per the i92.1 ruling.
    "PySide6.QtQuickControls2",
    "PySide6.QtMultimediaWidgets", "PySide6.QtSpatialAudio",
    "PySide6.Qt3DCore", "PySide6.Qt3DRender", "PySide6.Qt3DInput",
    "PySide6.Qt3DLogic", "PySide6.Qt3DAnimation", "PySide6.Qt3DExtras",
    "PySide6.QtCharts", "PySide6.QtDataVisualization", "PySide6.QtGraphs",
    "PySide6.QtPdf", "PySide6.QtPdfWidgets", "PySide6.QtSql",
    # PySide6.QtNetwork is NOT excluded: the QtMultimedia python binding
    # imports it at module level (verified 6.11.1), so excluding it makes
    # `import PySide6.QtMultimedia` an ImportError in the frozen app —
    # exactly what --bundle-self-check caught. The tracer itself never
    # touches QtNetwork; it ships only as QtMultimedia's import dependency.
    "PySide6.QtTest", "PySide6.QtBluetooth",
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

# ---------------------------------------------------------------------------
# QtMultimedia audio-only trim (fauxcasa-v46.3, per the fauxcasa-i92.1
# ruling, 2026-08-11): video playback is the SANDBOXED streaming seam —
# a separate worker process decodes with PyAV and streams frames/PCM
# through shared memory. QtMultimedia in-process DECODING of attacker
# bytes (QMediaPlayer/QVideoWidget + the Qt ffmpeg/WMF media backends)
# is DECLINED; QAudioSink consuming PCM that OUR worker produced is what
# decode-service.md §3c prescribes. So the bundle re-includes ONLY:
#   * the PySide6.QtMultimedia python module (QAudioSink/QAudioFormat/
#     QMediaDevices) and its shiboken binding,
#   * the core Qt6Multimedia library (QAudioSink's platform audio-output
#     code — WASAPI/PulseAudio/CoreAudio — is built INTO it, not into
#     the media-backend plugins),
# and strips the media DECODE backends the PyInstaller hook would drag
# in: the plugins/multimedia/* backend plugins (ffmpegmediaplugin,
# windowsmediaplugin, gstreamer*) and the Qt-bundled libav DLLs
# (avcodec/avformat/...) that exist only to serve them. We ship NO Qt
# ffmpeg plugin. The filter matches Qt-owned paths only, so PyAV's own
# libav wheels ("av/..." / "av.libs") — the sandboxed worker's decoder,
# v46.2 — are never touched. If a Qt release ever moves QAudioSink's
# platform code into a plugin this filter strips, the guarded
# viewer._make_audio_sink returns None and playback degrades to
# wall-clock silent video rather than crashing — honest, and the smoke
# gate's audio check (follow-up) would flag it.
_qtmm_veto = ("ffmpegmediaplugin", "windowsmediaplugin", "gstreamer",
              "avcodec", "avformat", "avdevice", "avfilter", "avutil",
              "swresample", "swscale")


def _keep_binary(entry):
    dest = entry[0].replace("\\", "/").lower()
    if "pyside6" not in dest and not dest.startswith("qt"):
        return True  # non-Qt binaries (incl. PyAV's own libav): keep all
    base = dest.rsplit("/", 1)[-1]
    return not any(t in base for t in _qtmm_veto)


a.binaries = [e for e in a.binaries if _keep_binary(e)]

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
