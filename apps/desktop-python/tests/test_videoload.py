"""Tests for videoload.py video poster-frame decode.

Split from test_tracer.py (fauxcasa-l09); originally lines 10257-10706 of the monolith."""

from __future__ import annotations

import json
from pathlib import Path
import pytest
import inmeta
import thumbcache
import metareader
from catalog import (
    ScanFilter,
    load_catalog,
    save_catalog,
    scan_library,
    walk_library,
)
from tracer_helpers import (
    REPO,
    _make_clip,
    _offscreen_app,
    _raw_catalog,
    _raw_photo_rows,
    _selection_grid,
    _thumb_qimage,
    make_jpeg,
)


# ---------------------------------------------------------------------------
# Video support (fauxcasa-v46.2): Picasa's documented video extension list in
# BOTH walkers (lockstep, or caches stop binding), PyAV poster-frame decode
# routed by extension ahead of any content sniff (per the merged decode-
# service design §3c: PyAV in-process, never an ffmpeg subprocess), ini
# attachment to video files (star/caption/albums/geotag + width=/height= dim
# seeds), corrupt-video fail-soft, media kind through the catalog round-trip,
# the grid's play badge, and the viewer's honest playback-pending note.
# Playback itself is fauxcasa-v46.3 (gated on the §3c sandbox-valve ruling).
#
# Fixture provenance (privacy rule: NEVER real family data): _make_clip
# encodes a tiny solid-color mpeg4 clip from scratch with PyAV — the same
# engine the poster seam decodes with — so every video fixture is synthetic
# and generated in-test.
# ---------------------------------------------------------------------------


def test_video_extensions_in_both_walkers(tmp_path: Path) -> None:
    """Picasa's documented video list (files-supported-by-picasa3.md "For
    playback in Picasa": 17 extensions, plus .mpeg as the four-letter
    alias of .mpg — the .jpeg/.jpg precedent) is in BOTH EXTS sets, in
    lockstep; audio-only .wma/.mp3 stay excluded; both walks pick video
    files up case-insensitively; and the size scan-filter always KEEPS
    videos (their dims are unknowable without a decode)."""
    import importlib.util

    import catalog
    import videoload

    spec = importlib.util.spec_from_file_location(
        "mtc", REPO / "scripts" / "make-thumbcache.py")
    mtc = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mtc)

    documented = {".mpg", ".mpeg", ".mod", ".mmv", ".tod", ".wmv", ".asf",
                  ".avi", ".divx", ".mov", ".m4v", ".3gp", ".3g2", ".mp4",
                  ".m2t", ".m2ts", ".mts", ".mkv"}
    assert videoload.VIDEO_EXTS == documented
    assert mtc.VIDEO_EXTS == videoload.VIDEO_EXTS  # the script's mirror
    assert catalog.EXTS == mtc.EXTS                # the whole lockstep set
    assert documented <= catalog.EXTS
    assert not (catalog.EXTS & {".wma", ".mp3"})   # audio is not walked

    root = tmp_path / "lib"
    root.mkdir()
    for name in ("a.MP4", "b.avi", "c.MoV", "d.m2ts"):
        (root / name).write_bytes(b"stub")         # walk checks suffix only
    make_jpeg(root / "e.jpg")
    walked = [p.name for p in walk_library(root)]
    assert sorted(walked) == ["a.MP4", "b.avi", "c.MoV", "d.m2ts", "e.jpg"]
    script_walk = sorted(p for p in root.rglob("*")
                         if p.suffix.lower() in mtc.EXTS and p.is_file())
    assert [p.name for p in script_walk] == walked

    # the size filter judges video dims unknowable and keeps every video
    # (QImageReader must never sniff video bytes — videoload module doc)
    kept = {p.name for p in walk_library(root, ScanFilter(min_width=10000))}
    assert kept == {"a.MP4", "b.avi", "c.MoV", "d.m2ts"}


def test_video_poster_thumb_media_kind_and_duration(tmp_path: Path) -> None:
    """A real clip indexes to a poster-frame thumbnail via PyAV: the cached
    thumb has the clip's dimensions (64x48 < 256: never upscaled) and its
    solid frame color; a sub-second clip exercises the seek-past-the-end
    fallback to the first decodable frame; media kind is set by extension;
    sha256/size/mtime identity fills as usual; and the videoload seam's
    poster_frame returns the packed fixed-shape RGB buffer plus a sane
    probe_duration."""
    import videoload

    root = tmp_path / "lib"
    _make_clip(root / "clip.mp4", color=(200, 60, 40))            # 1 s
    _make_clip(root / "short.avi", color=(40, 60, 200), nframes=2)  # 0.25 s
    make_jpeg(root / "still.jpg")
    cat = scan_library(root)
    assert {p.rel: p.media for p in cat.photos} == {
        "clip.mp4": "video", "short.avi": "video", "still.jpg": "image"}

    cache = thumbcache.load_cache(
        thumbcache.build_cache(cat, tmp_path / "c").path)
    by = dict(zip(cache.files, cache.entries))
    _o, length, w, h = by["clip.mp4"]
    assert length > 0 and (w, h) == (64, 48)
    img = _thumb_qimage(cache, cache.files.index("clip.mp4"))
    px = img.pixelColor(32, 24)
    # solid (200, 60, 40) through yuv420p + JPEG q80; allow codec drift
    assert abs(px.red() - 200) < 40 and px.red() > px.blue()

    _o, length, w, h = by["short.avi"]                 # fallback path
    assert length > 0 and (w, h) == (64, 48)
    px = _thumb_qimage(cache, cache.files.index("short.avi")) \
        .pixelColor(32, 24)
    assert px.blue() > px.red()                        # the blue clip

    for p in cat.photos:                               # N6 identity as usual
        assert p.sha256 and p.size > 0 and p.mtime > 0

    # the seam's raw shape: packed RGB888, exactly w*3*h bytes (the fixed-
    # shape pixel contract the sandboxed decode service will validate)
    data = (root / "clip.mp4").read_bytes()
    buf, w, h = videoload.poster_frame(data)
    assert (w, h) == (64, 48) and len(buf) == w * 3 * h
    dur = videoload.probe_duration(data)
    assert dur is not None and 0.5 <= dur <= 2.0
    assert videoload.poster_frame(b"not a video") is None
    assert videoload.probe_duration(b"not a video") is None


def test_video_corrupt_is_error_tile(tmp_path: Path) -> None:
    """Corrupt videos — outright garbage bytes under two video extensions —
    yield the existing zero-length error tile and never abort the build;
    the good neighbors (a still AND a decodable clip) still index."""
    root = tmp_path / "lib"
    root.mkdir()
    (root / "garbage.mp4").write_bytes(b"\x00\x01 not a video" * 64)
    (root / "noise.wmv").write_bytes(b"\xff\xd8 also not one" * 64)
    _make_clip(root / "ok.avi")
    make_jpeg(root / "ok.jpg")
    cat = scan_library(root)
    cache = thumbcache.load_cache(
        thumbcache.build_cache(cat, tmp_path / "c").path)
    lengths = {rel: length for rel, (_o, length, _w, _h) in
               zip(cache.files, cache.entries)}
    assert lengths["garbage.mp4"] == 0         # error tile
    assert lengths["noise.wmv"] == 0           # error tile
    assert lengths["ok.avi"] > 0               # neighbors unharmed
    assert lengths["ok.jpg"] > 0


def test_video_ini_attachment_and_dim_seed(tmp_path: Path) -> None:
    """ini sections for video files flow through the existing parse:
    star/caption/albums/geotag attach by filename exactly like a photo's,
    width=/height= seed Photo.dims (malformed values fail soft to None) —
    and the indexer's SKIPPED in-file metadata pass (exiv2 video support
    is patchy) leaves the ini values in force after a build."""
    root = tmp_path / "lib"
    _make_clip(root / "clip00.avi")
    _make_clip(root / "clip01.mp4")
    make_jpeg(root / "p.jpg")
    uid = "d4e5f60718293a4b5c6d7e8f90a1b2c3"
    (root / ".picasa.ini").write_text(
        f"[.album:{uid}]\r\nname=Movies\r\n"
        "[clip00.avi]\r\nstar=yes\r\ncaption=First swim\r\n"
        f"albums={uid}\r\ngeotag=48.858844,2.294351\r\n"
        "width=640\r\nheight=480\r\n"
        "[clip01.mp4]\r\nwidth=banana\r\nheight=480\r\n")
    cat = scan_library(root)
    a = next(p for p in cat.photos if p.rel == "clip00.avi")
    assert a.media == "video" and a.star == 1
    assert a.caption == "First swim"
    assert a.albums == (uid,)
    assert a.geotag == pytest.approx((48.858844, 2.294351))
    assert a.dims == (640, 480)
    assert cat.albums[uid].members == [cat.photos.index(a)]
    b = next(p for p in cat.photos if p.rel == "clip01.mp4")
    assert b.dims is None                      # malformed width= fails soft

    assert thumbcache.build_cache(cat, tmp_path / "c") is not None
    assert a.caption == "First swim" and a.star == 1  # ini stays in force


def test_video_catalog_roundtrip_media_and_dims(tmp_path: Path) -> None:
    """media kind and ini-seeded dims survive save_catalog/load_catalog:
    dims persist (`wh` rows — the warm path never re-reads inis) while
    media is DERIVED from the extension on load, never stored; and a
    pre-v6 catalog (walked without videos) is rejected so a warm start
    can never silently hide every video in the library."""
    import catalog as catmod

    root = tmp_path / "lib"
    _make_clip(root / "c.mp4")
    make_jpeg(root / "p.jpg")
    (root / ".picasa.ini").write_text("[c.mp4]\r\nwidth=64\r\nheight=48\r\n")
    cat = scan_library(root)
    assert thumbcache.build_cache(cat, tmp_path / "cc") is not None
    path = tmp_path / "catalog.json"
    save_catalog(cat, path)

    loaded = load_catalog(path, root)
    assert loaded is not None
    v = next(p for p in loaded.photos if p.rel == "c.mp4")
    assert v.media == "video" and v.dims == (64, 48)
    assert v.sha256 == next(p for p in cat.photos
                            if p.rel == "c.mp4").sha256
    s = next(p for p in loaded.photos if p.rel == "p.jpg")
    assert s.media == "image" and s.dims is None
    data = _raw_catalog(path)
    rows = _raw_photo_rows(data)
    assert all("media" not in r for r in rows)  # derived, never persisted

    # A hard-coded pre-v6 (pre-video) version, not "CATALOG_VERSION - N":
    # CATALOG_VERSION - 1 is the multiroot-compat boundary (fauxcasa-
    # ed5.7.2 — a single-root library still loads it, see
    # test_multiroot_old_version_single_root_still_loads) and
    # CATALOG_VERSION - 2 is now PRE_MULTIROOT_VERSION itself (also
    # single-root-accepted) since the ed5.5 zstd bump — any relative
    # offset risks silently landing on a future compat carve-out again.
    assert 4 not in (catmod.CATALOG_VERSION, catmod.PRE_MULTIROOT_VERSION)
    data["version"] = 4
    path.write_text(json.dumps(data))  # plain JSON: an old format never zstd-wrapped
    assert load_catalog(path, root) is None     # pre-video: cold-rebuild


def test_grid_video_play_badge_paint_smoke(tmp_path: Path) -> None:
    """The video play badge paints in its OWN corner (bottom-left; star
    owns top-right, geotag pin bottom-right) without incident alongside
    both other badges: the badge-center pixel of a video tile is the play
    glyph's white, the same spot on a non-video neighbor is not."""
    from PySide6.QtGui import QImage

    from grid import _play_polygon

    g = _selection_grid(tmp_path)
    cat = g.catalog
    d = g.display
    cat.photos[d[0]].media = "video"
    cat.photos[d[0]].star = 2                       # all three corners at once
    cat.photos[d[0]].geotag = (60.72125, -135.05685)
    shot = g.viewport().grab().toImage().convertToFormat(
        QImage.Format.Format_RGB32)
    assert not shot.isNull()

    s = max(7.0, g.tile / 14.0)

    def badge_px(n: int):
        r = g._item_rect(g.groups[0], n)            # scroll is 0: same coords
        c = shot.pixelColor(int(r.x() + s + 2), int(r.bottom() - s - 2))
        return (c.red(), c.green(), c.blue())

    assert all(abs(v - 235) < 25 for v in badge_px(0))      # the play glyph
    assert not all(abs(v - 235) < 25 for v in badge_px(1))  # plain neighbor

    # shape sanity: a right-pointing triangle — apex at the vertical center
    poly = _play_polygon(10.0, 10.0, 8.0)
    assert poly.size() == 3
    assert poly.at(1).x() > poly.at(0).x() and poly.at(1).y() == 10.0


def test_viewer_video_poster_and_pending_note(tmp_path: Path) -> None:
    """viewer.load_original routes video by extension to the poster seam:
    a clip's poster decodes at native size (proven by dimensions AND the
    frame color), the Picasa rotate= turns compose on top exactly like any
    format, a corrupt video returns a null QImage (fail-soft), and the
    viewer's info line carries the honest video chip — the poster is
    shown with the P-plays hint (v46.3 replaced the old "playback
    pending" placeholder); playback never starts until asked."""
    _offscreen_app()
    from viewer import ViewerPage, load_original

    root = tmp_path / "lib"
    clip = _make_clip(root / "clip.mp4", color=(200, 60, 40))
    make_jpeg(root / "p.jpg")
    img = load_original(str(clip), 0)
    assert (img.width(), img.height()) == (64, 48)
    px = img.pixelColor(32, 24)
    assert abs(px.red() - 200) < 40 and px.red() > px.blue()
    img = load_original(str(clip), 1)          # rotate= composes on top
    assert (img.width(), img.height()) == (48, 64)

    bad = root / "bad.avi"
    bad.write_bytes(b"garbage" * 100)
    assert load_original(str(bad), 0).isNull()

    cat = scan_library(root)
    v = ViewerPage(cat, None)
    v.resize(320, 240)
    v.show()
    idx = next(i for i, p in enumerate(cat.photos) if p.rel == "clip.mp4")
    v.show_photo([idx], 0)
    txt = v._info_text(cat.photos[idx])
    assert "video" in txt and "P plays" in txt   # honest chip + play hint
    assert "playback pending" not in txt         # the placeholder is gone
    assert v._video is None                      # playback never auto-starts
    still = next(p for p in cat.photos if p.rel == "p.jpg")
    assert "video" not in v._info_text(still)
    assert not v.grab().isNull()               # the chrome paints w/o incident
    v.quiesce()


def test_make_thumbcache_video_paths(tmp_path: Path) -> None:
    """The standalone PIL builder mirrors the same routing (in PyAV+PIL
    terms): a real clip thumbs to its poster frame at native size with the
    frame's color; corrupt video bytes are the error tile."""
    import importlib.util
    import io

    from PIL import Image

    spec = importlib.util.spec_from_file_location(
        "mtc", REPO / "scripts" / "make-thumbcache.py")
    mtc = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mtc)

    clip = _make_clip(tmp_path / "c.mp4", color=(200, 60, 40))
    (blob, w, h), = mtc._make_thumb(clip, [256])
    assert blob and (w, h) == (64, 48)
    px = Image.open(io.BytesIO(blob)).getpixel((32, 24))
    assert abs(px[0] - 200) < 40 and px[0] > px[2]

    bad = tmp_path / "bad.wmv"
    bad.write_bytes(b"not a movie")
    assert mtc._make_thumb(bad, [256]) == [(b"", 0, 0)]


# ---------------------------------------------------------------------------
# Video date_taken + streaming hash (fauxcasa-v46.6): probe_creation_time
# reads container metadata via PyAV; video indexing streams sha256 instead
# of loading whole-file bytes; §4 fill-when-empty rule for video dates.
# ---------------------------------------------------------------------------


def test_probe_creation_time(tmp_path: Path) -> None:
    """probe_creation_time returns a canonical 'YYYY-MM-DDTHH:MM:SS' string
    when the container carries creation_time metadata; returns None for
    garbage bytes or a nonexistent path; agrees between path and bytes."""
    import av
    import videoload

    # Clip with an explicit creation_time in container metadata.
    clip = _make_clip(tmp_path / "ts.mp4",
                      creation_time="2023-05-15T10:30:00.000000Z")

    # Verify what the muxer actually wrote, then compare to probe output.
    with av.open(str(clip)) as c:
        raw = c.metadata.get("creation_time")
    if raw is not None:
        # Muxer preserved it: probe must canonicalise it.
        dt = videoload.probe_creation_time(clip)
        assert dt == "2023-05-15T10:30:00", (
            f"probe returned {dt!r}; raw container value was {raw!r}")
        # Path and bytes sources must agree.
        assert videoload.probe_creation_time(clip.read_bytes()) == dt

    # Fail-soft: garbage bytes must not raise.
    assert videoload.probe_creation_time(b"not a video") is None
    # Fail-soft: nonexistent path must not raise.
    assert videoload.probe_creation_time(tmp_path / "nosuch.mp4") is None


def test_parse_creation_time_rejects_malformed() -> None:
    """_parse_creation_time validates the calendar, not just punctuation:
    all-zero placeholders and out-of-range fields (real camera output)
    must return None so date grouping falls back to mtime, never sorting
    a fake date (codex cross-review finding, fauxcasa-v46.6)."""
    from videoload import _parse_creation_time

    assert _parse_creation_time("2023-05-15T10:30:00.000000Z") == \
        "2023-05-15T10:30:00"
    assert _parse_creation_time("2023-05-15 10:30:00+05:00") == \
        "2023-05-15T10:30:00"
    assert _parse_creation_time("0000-00-00T00:00:00Z") is None
    assert _parse_creation_time("2023-99-99T99:99:99Z") is None
    assert _parse_creation_time("2023-02-30T10:30:00") is None
    assert _parse_creation_time("") is None
    assert _parse_creation_time(None) is None


def test_video_date_taken_fill_when_empty(tmp_path: Path) -> None:
    """apply_photo_meta fills date_taken only when the field is empty (§4
    fill-when-empty rule for probe_creation_time): an existing value —
    e.g. from a prior index or a future ini source — must be preserved."""
    from catalog import Photo
    from metareader import FileMeta

    # Gap-fill: date_taken was None — probe result must be accepted.
    p_empty = Photo(rel="a.mp4", folder="", name="a.mp4")
    fmeta = FileMeta(date_taken="2023-05-15T10:30:00")
    thumbcache.apply_photo_meta(p_empty, 100, 1000, "a" * 64,
                                inmeta.EMPTY, fmeta)
    assert p_empty.date_taken == "2023-05-15T10:30:00"

    # Existing value preserved: probe must not override.
    p_set = Photo(rel="b.mp4", folder="", name="b.mp4")
    p_set.date_taken = "2020-01-01T12:00:00"
    thumbcache.apply_photo_meta(p_set, 100, 1000, "a" * 64,
                                inmeta.EMPTY, fmeta)
    assert p_set.date_taken == "2020-01-01T12:00:00"


def test_video_index_no_readbytes(tmp_path: Path, monkeypatch) -> None:
    """Video indexing must stream sha256 from disk; Path.read_bytes must
    not be called for any video file (multi-GB videos would spike RSS).
    Photo files still use the read_bytes path unchanged."""
    root = tmp_path / "lib"
    _make_clip(root / "clip.mp4")
    make_jpeg(root / "still.jpg")
    cat = scan_library(root)

    called: set[str] = set()
    real_rb = Path.read_bytes

    def _spy(self: Path) -> bytes:
        called.add(self.name)
        return real_rb(self)

    monkeypatch.setattr(Path, "read_bytes", _spy)
    thumbcache.build_cache(cat, tmp_path / "c")

    assert "clip.mp4" not in called   # streaming hash: no read_bytes for video
    assert "still.jpg" in called       # photo path unchanged


def test_video_read_photo_meta_src_none(tmp_path: Path) -> None:
    """read_photo_meta's video branch, src=None (multiroot unresolved/
    offline root — bead .e): fails soft exactly like the still-photo
    OSError path — empty bytes, -1 signals, the empty-string sha256, no
    date_taken (probe_creation_time never runs without a path)."""
    import hashlib

    from catalog import Photo

    photo = Photo(rel="clip.mp4", folder="", name="clip.mp4")
    data, size, mtime, sha, meta, fmeta = thumbcache.read_photo_meta(
        None, photo)
    assert data == b""
    assert (size, mtime) == (-1, -1)
    assert sha == hashlib.sha256(b"").hexdigest()
    assert meta is inmeta.EMPTY
    assert fmeta is metareader.EMPTY
    assert fmeta.date_taken is None
