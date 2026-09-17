"""Tests for catalog.py scanning, persistence, and reconciliation.

Split from test_tracer.py (fauxcasa-l09); originally lines 267-1217 of the monolith."""

from __future__ import annotations

import json
import struct
from pathlib import Path
import pytest
import library as libmod
import thumbcache
from catalog import (
    ScanFilter,
    load_catalog,
    reconcile_walk,
    save_catalog,
    save_catalog_retrying,
    scan_library,
    walk_library,
)
from tracer_helpers import (
    _load_mtc,
    _offscreen_app,
    _raw_catalog,
    _raw_photo_rows,
    _selection_grid,
    _write_faces_contacts_xml,
    _write_raw_catalog,
    make_jpeg,
)


def test_scan_metadata(library: Path) -> None:
    cat = scan_library(library)
    # walk order is sorted rel path: .picasaoriginals/a, a, b, c
    rels = [p.rel for p in cat.photos]
    assert rels == sorted(rels)
    assert len(cat.photos) == 4

    a = next(p for p in cat.photos if p.rel.endswith("Trip/a.jpg"))
    assert a.star and a.caption == "the beach"
    assert a.keywords == ("sun", "sand")
    assert a.rotate == 1
    assert a.albums == ("deadbeefdeadbeefdeadbeefdeadbeef",)
    assert a.visible

    b = next(p for p in cat.photos if p.rel.endswith("b.jpg"))
    assert b.hidden and not b.visible

    stashed = next(p for p in cat.photos if ".picasaoriginals" in p.rel)
    assert not stashed.visible

    assert cat.visible_count == 2  # a.jpg + c.jpg
    # folder title is the ON-DISK name (N1) — ini name= goes stale
    assert cat.folders["2020-01-01 Trip"].title == "2020-01-01 Trip"
    assert cat.folders["2020-01-01 Trip"].description == "fun"
    assert cat.folders["2020-01-01 Trip"].photo_count == 1

    album = cat.albums["deadbeefdeadbeefdeadbeefdeadbeef"]
    assert album.name == "Best Of"
    assert album.members == [rels.index("2020-01-01 Trip/a.jpg")]


def test_walk_rule_parity_with_make_thumbcache(library: Path) -> None:
    """catalog order must equal make-thumbcache entry order or caches
    stop binding — compare against the script's own walk."""
    import catalog

    mtc = _load_mtc()
    assert mtc.EXTS == catalog.EXTS  # the usual drift vector
    script_walk = mtc.walk_library(library, mtc.EXTS)
    assert walk_library(library) == script_walk


def test_component_sort_order(tmp_path: Path) -> None:
    """The entry-order rule is path-COMPONENT order: '2020' sorts before
    '2020-01 Trip' even though the joined strings sort the other way
    ('-' < '/'). The shipped benchmark cache uses this order."""
    root = tmp_path / "lib"
    make_jpeg(root / "2020" / "x.jpg")
    make_jpeg(root / "2020-01 Trip" / "x.jpg")
    rels = [p.rel for p in scan_library(root).photos]
    assert rels == ["2020/x.jpg", "2020-01 Trip/x.jpg"]
    assert rels != sorted(rels)  # string sort would invert them


def test_scan_filter_ignores_images_outside_dimension_bounds(
        tmp_path: Path) -> None:
    root = tmp_path / "lib"
    make_jpeg(root / "icons" / "tiny.jpg", 32, 32)
    make_jpeg(root / "photos" / "normal.jpg", 640, 480)
    make_jpeg(root / "source" / "huge.jpg", 2400, 1600)

    cat = scan_library(root, ScanFilter(min_width=100, min_height=100,
                                        max_width=2000, max_height=1200))
    assert [p.rel for p in cat.photos] == ["photos/normal.jpg"]
    assert cat.visible_count == 1
    assert list(cat.folders) == ["photos"]


def test_scan_filter_keeps_unreadable_images_for_error_tiles(
        tmp_path: Path) -> None:
    root = tmp_path / "lib"
    bad = root / "broken.jpg"
    bad.parent.mkdir(parents=True)
    bad.write_bytes(b"not actually a jpeg")

    cat = scan_library(root, ScanFilter(min_width=100, min_height=100))
    assert [p.rel for p in cat.photos] == ["broken.jpg"]


def test_filtered_cache_dir_is_separate_from_unfiltered(tmp_path: Path) -> None:
    from thumbcache import cache_dir_for

    root = tmp_path / "lib"
    root.mkdir()
    cache_root = tmp_path / "cache"
    key = str(root.resolve())
    plain = cache_dir_for(key, cache_root)
    filtered = cache_dir_for(
        key, cache_root, ScanFilter(min_width=100, min_height=100).cache_key())
    assert plain != filtered
    assert cache_dir_for(key, cache_root) == plain


def test_rel_paths_match_relative_to(tmp_path: Path) -> None:
    """rel_paths() must equal relative_to().as_posix() exactly —
    including for true roots ('/', 'D:\\', UNC shares), whose str()
    keeps a trailing separator that breaks naive prefix slicing."""
    from pathlib import PurePosixPath, PureWindowsPath

    from catalog import rel_paths

    # normal nested root (fast path)
    root = tmp_path / "lib"
    make_jpeg(root / "f" / "a.jpg")
    files = [root / "f" / "a.jpg"]
    assert rel_paths(root, files) == ["f/a.jpg"]

    # filesystem root: trailing-separator prefix
    proot = PurePosixPath("/")
    pfiles = [PurePosixPath("/a/b.jpg"), PurePosixPath("/c.jpg")]
    assert rel_paths(proot, pfiles) == ["a/b.jpg", "c.jpg"]

    # Windows drive root: native separator differs from POSIX (on a
    # POSIX host this also exercises the parity-probe fallback)
    wroot = PureWindowsPath("P:/")
    wfiles = [PureWindowsPath("P:/photos/a.jpg")]
    assert rel_paths(wroot, wfiles) == ["photos/a.jpg"]

    # UNC share root
    uroot = PureWindowsPath("//nas/photos")
    ufiles = [PureWindowsPath("//nas/photos/DCIM/x.jpg")]
    assert rel_paths(uroot, ufiles) == ["DCIM/x.jpg"]


def test_duplicate_sections_and_flag_normalization(tmp_path: Path) -> None:
    root = tmp_path / "lib"
    make_jpeg(root / "f" / "a.jpg")
    make_jpeg(root / "f" / "Originals" / "a.jpg")  # legacy stash
    (root / "f" / ".picasa.ini").write_text(
        "[a.jpg]\r\ncaption=first\r\n"
        "[A.JPG]\r\nstar=Yes \r\nkeywords=dup\r\n"  # dup section, odd case
    )
    cat = scan_library(root)
    a = next(p for p in cat.photos if p.rel == "f/a.jpg")
    assert a.caption == "first"  # first occurrence wins
    assert a.star  # merged from the duplicate section, value normalized
    assert a.keywords == ("dup",)
    legacy = next(p for p in cat.photos if "Originals" in p.rel)
    assert not legacy.visible


def test_stashed_original_association(library: Path) -> None:
    """scan_library links each stash copy to its same-named visible sibling
    by catalog index (fauxcasa-cam.19, M1 read-only association)."""
    cat = scan_library(library)
    a = next(p for p in cat.photos if p.rel == "2020-01-01 Trip/a.jpg")
    # a.jpg has a same-named copy in .picasaoriginals/ — must be linked
    assert a.stashed_original is not None
    assert cat.photos[a.stashed_original].rel == \
        "2020-01-01 Trip/.picasaoriginals/a.jpg"
    # b.jpg has no stash copy; c.jpg is in a different folder entirely
    b = next(p for p in cat.photos if p.rel == "2020-01-01 Trip/b.jpg")
    assert b.stashed_original is None
    c = next(p for p in cat.photos if p.rel == "2021-05-05 Picnic/c.jpg")
    assert c.stashed_original is None
    # the stash copy itself is never the target of a link
    stash = next(p for p in cat.photos
                 if p.rel == "2020-01-01 Trip/.picasaoriginals/a.jpg")
    assert stash.stashed_original is None


def test_stashed_original_legacy_orphan_and_dotfolder(tmp_path: Path) -> None:
    """Legacy Originals/ name links the same way as .picasaoriginals/;
    an orphaned stash file (sibling gone) links nothing; an arbitrary
    dot-folder is hidden but never associates (fauxcasa-cam.19)."""
    root = tmp_path / "lib"
    # legacy name: f/a.jpg + f/Originals/a.jpg => linked
    make_jpeg(root / "f" / "a.jpg")
    make_jpeg(root / "f" / "Originals" / "a.jpg")
    # orphaned stash: no sibling => no link, no exception
    make_jpeg(root / "f" / "Originals" / "orphan.jpg")
    # arbitrary dot-folder: hides but must NOT associate
    make_jpeg(root / "g" / "b.jpg")
    make_jpeg(root / "g" / ".thumbnails" / "b.jpg")
    # root-level pair: folder="" edge case
    make_jpeg(root / "r.jpg")
    make_jpeg(root / ".picasaoriginals" / "r.jpg")

    cat = scan_library(root)
    a = next(p for p in cat.photos if p.rel == "f/a.jpg")
    assert a.stashed_original is not None
    assert "Originals" in cat.photos[a.stashed_original].rel

    orphan = next(p for p in cat.photos if p.rel == "f/Originals/orphan.jpg")
    assert orphan.stashed_original is None  # it is the stash, not the visible
    # the visible sibling is gone, so no photo has a stashed_original for orphan
    assert not any(p.stashed_original is not None and
                   cat.photos[p.stashed_original].rel == "f/Originals/orphan.jpg"
                   for p in cat.photos)

    b = next(p for p in cat.photos if p.rel == "g/b.jpg")
    assert b.stashed_original is None  # .thumbnails must NOT associate

    r = next(p for p in cat.photos if p.rel == "r.jpg")
    assert r.stashed_original is not None
    assert cat.photos[r.stashed_original].rel == ".picasaoriginals/r.jpg"


def test_stashed_original_survives_catalog_roundtrip(
        library: Path, tmp_path: Path) -> None:
    """The stashed_original association survives a save/load cycle — it is
    re-derived on load_catalog, never persisted (fauxcasa-cam.19)."""
    cat = scan_library(library)
    path = tmp_path / "catalog.json"
    save_catalog(cat, path)

    loaded = load_catalog(path, library)
    assert loaded is not None
    a = next(p for p in loaded.photos if p.rel == "2020-01-01 Trip/a.jpg")
    assert a.stashed_original is not None
    assert loaded.photos[a.stashed_original].rel == \
        "2020-01-01 Trip/.picasaoriginals/a.jpg"
    # derived, not stored — the JSON must contain no stashed_original key
    assert '"stashed_original"' not in json.dumps(_raw_catalog(path))


def test_build_fills_signals_and_binds(library: Path, tmp_path: Path) -> None:
    cat = scan_library(library)
    cache_dir = tmp_path / "cache"
    result = thumbcache.build_cache(cat, cache_dir)
    assert result is not None
    assert result.photos == 4 and result.rate > 0  # throughput measured

    cache = thumbcache.load_cache(result.path)
    assert cache.count == 4
    thumbcache.bind(cache, cat)  # must not raise

    # the indexer filled identity + staleness signals into the catalog
    for p in cat.photos:
        assert p.size >= 0 and p.mtime >= 0
        assert p.sha256 is not None and len(p.sha256) == 64

    # blobs are real JPEGs with recorded dims
    with open(result.path, "rb") as f:
        for off, length, w, h in cache.entries:
            assert length > 0 and w > 0 and h > 0
            f.seek(off)
            assert f.read(3) == b"\xff\xd8\xff"

    # library drift => bind refuses an out-of-date fcache
    make_jpeg(library / "2021-05-05 Picnic" / "d.jpg")
    cat2 = scan_library(library)
    with pytest.raises(thumbcache.CacheError):
        thumbcache.bind(cache, cat2)


def test_persistent_catalog_roundtrip(library: Path, tmp_path: Path) -> None:
    """A loaded catalog is indistinguishable from a freshly walked one,
    so a warm start can skip the walk."""
    cat = scan_library(library)
    thumbcache.build_cache(cat, tmp_path / "c")  # fills signals
    path = tmp_path / "catalog.json"
    save_catalog(cat, path)

    loaded = load_catalog(path, library)
    assert loaded is not None
    assert [p.rel for p in loaded.photos] == [p.rel for p in cat.photos]
    assert loaded.visible_count == cat.visible_count
    assert set(loaded.folders) == set(cat.folders)
    assert set(loaded.albums) == set(cat.albums)
    a = next(p for p in loaded.photos if p.rel.endswith("Trip/a.jpg"))
    assert a.star and a.caption == "the beach" and a.rotate == 1
    assert a.sha256 is not None  # signals survive the round trip
    # derived fields recomputed, not stored
    assert a.folder == "2020-01-01 Trip" and a.visible
    b = next(p for p in loaded.photos if p.rel.endswith("b.jpg"))
    assert b.hidden and not b.visible
    # folder description persisted; title derived from the on-disk name
    assert loaded.folders["2020-01-01 Trip"].description == "fun"
    assert loaded.folders["2020-01-01 Trip"].title == "2020-01-01 Trip"
    assert loaded.albums["deadbeefdeadbeefdeadbeefdeadbeef"].name == "Best Of"


def test_load_catalog_rejects_foreign_format(tmp_path: Path) -> None:
    p = tmp_path / "catalog.json"
    p.write_text(json.dumps({"library": "x", "files": []}))  # old signals-only
    assert load_catalog(p, tmp_path) is None
    p.write_text(json.dumps([1, 2, 3]))  # not even an object
    assert load_catalog(p, tmp_path) is None
    p.write_text("{ not json")
    assert load_catalog(p, tmp_path) is None
    assert load_catalog(tmp_path / "missing.json", tmp_path) is None


# ---- on-disk size budget (fauxcasa-ed5.5, spec §10 item 20 re-baseline):
# zstd level 3 over folder-grouped compact JSON — see
# docs/research/catalog-size-analysis.md and catalog.save_catalog. --------


def test_catalog_v13_is_zstd_compressed_and_smaller_than_plain(
        library: Path, tmp_path: Path) -> None:
    """save_catalog's on-disk file starts with the zstd frame magic and is
    smaller than an uncompressed dump of the exact same (grouped) JSON —
    the size-budget mechanism actually engages, not just parses back."""
    import catalog as catmod

    cat = scan_library(library)
    thumbcache.build_cache(cat, tmp_path / "c")  # fills z/m/x so rows aren't tiny
    path = tmp_path / "catalog.json"
    save_catalog(cat, path)

    raw = path.read_bytes()
    assert raw[:4] == catmod.ZSTD_MAGIC
    uncompressed = json.dumps(_raw_catalog(path),
                              separators=(",", ":")).encode("utf-8")
    assert len(raw) < len(uncompressed)


def test_load_catalog_accepts_legacy_v11_plain_json(tmp_path: Path) -> None:
    """A hand-built pre-multiroot (v11) plain-JSON catalog — flat rows, no
    "R", no zstd wrapping — still loads for a single-root library (design
    §5's compat carve-out), unaffected by the ed5.5 v13 zstd/grouped bump:
    a v11 file was never grouped or compressed to begin with."""
    import catalog as catmod

    p = tmp_path / "catalog.json"
    digest_hex = "ab" * 32  # 64-char hex, this format's native shape
    data = {
        "version": catmod.PRE_MULTIROOT_VERSION,
        "library": str(tmp_path),
        "photos": [{"r": "a.jpg", "z": 10, "m": 100, "x": digest_hex}],
        "folders": {}, "hidden_folders": [], "contacts": {},
        "db3_contacts": [], "albums": [],
    }
    p.write_text(json.dumps(data))
    loaded = load_catalog(p, tmp_path)
    assert loaded is not None
    assert loaded.photos[0].rel == "a.jpg"
    assert loaded.photos[0].sha256 == digest_hex


def test_load_catalog_rejects_plain_v12_json(tmp_path: Path) -> None:
    """A plain (uncompressed) v12 file — the format this catalog shipped
    right before the ed5.5 zstd bump — now simply fails the version gate:
    there is no migration path for it, only a cold rebuild (the catalog
    is a regenerable cache, not a document worth migrating)."""
    p = tmp_path / "catalog.json"
    data = {"version": 12, "library": str(tmp_path), "photos": [],
            "folders": {}, "hidden_folders": [], "contacts": {},
            "db3_contacts": [], "albums": []}
    p.write_text(json.dumps(data))
    assert load_catalog(p, tmp_path) is None


def test_load_catalog_rejects_corrupt_or_truncated_zstd(
        library: Path, tmp_path: Path) -> None:
    """A truncated zstd frame, and a file whose first 4 bytes lie about
    being a valid one, both degrade to None (-> cold walk) — never an
    uncaught exception reaching main()."""
    cat = scan_library(library)
    path = tmp_path / "catalog.json"
    save_catalog(cat, path)

    good = path.read_bytes()
    path.write_bytes(good[:len(good) // 2])       # truncated mid-frame
    assert load_catalog(path, library) is None

    path.write_bytes(good[:4] + b"not a real zstd payload at all, just junk")
    assert load_catalog(path, library) is None

    path.write_bytes(b"")                          # empty file
    assert load_catalog(path, library) is None


def test_catalog_sha256_b85_roundtrips_to_same_hex(tmp_path: Path) -> None:
    """"x" is stored as base85 of the raw 32 bytes on disk (shorter than
    64 hex chars) but load_catalog decodes it straight back to the exact
    same hex string Photo.sha256 has always carried — every other
    consumer of that field is unaffected by the encoding change."""
    import hashlib

    from catalog import Catalog, Folder, Photo

    digest_hex = hashlib.sha256(b"hello world").hexdigest()
    photos = [Photo(rel="a.jpg", folder="", name="a.jpg", sha256=digest_hex,
                    size=10, mtime=100)]
    folders = {"": Folder(rel="", title="x", photo_count=1, total_count=1)}
    cat = Catalog(root=tmp_path, photos=photos, folders=folders, albums={})
    path = tmp_path / "catalog.json"
    save_catalog(cat, path)

    loaded = load_catalog(path, tmp_path)
    assert loaded is not None
    assert loaded.photos[0].sha256 == digest_hex

    # On disk it's genuinely base85, not hex: shorter, and not the hex text.
    group_row = _raw_catalog(path)["photos"][0]["ph"][0]
    assert group_row["x"] != digest_hex
    assert len(group_row["x"]) < len(digest_hex)
    # ...but the ungrouping helper (what load_catalog itself uses) restores
    # the exact hex string.
    assert _raw_photo_rows(_raw_catalog(path))[0]["x"] == digest_hex


def test_catalog_multiroot_two_roots_round_trip_root_id(tmp_path: Path) -> None:
    """A minimal two-root case alongside the size-budget tests above:
    photos on two different roots each keep their own root_id across a
    save/load cycle, via the group-level "R" (fauxcasa-ed5.5) — the fuller
    header/folder-key coverage lives in
    test_multiroot_two_root_save_load_roundtrip_v13."""
    from catalog import Catalog, Photo

    root_a, root_b = tmp_path / "a", tmp_path / "b"
    root_a.mkdir()
    root_b.mkdir()
    id_a, id_b = "aaaaaaaa", "bbbbbbbb"
    cat = Catalog(
        root=root_a,
        photos=[Photo(rel="x.jpg", folder="", name="x.jpg", root_id=id_a),
                Photo(rel="x.jpg", folder="", name="x.jpg", root_id=id_b)],
        folders={}, albums={},
        roots=[libmod.LibraryRoot(id=id_a, path=root_a),
               libmod.LibraryRoot(id=id_b, path=root_b)],
        library_id=libmod.mint_library_id(),
    )
    path = tmp_path / "catalog.json"
    save_catalog(cat, path)

    cfg = libmod.LibraryConfig(library_id=cat.library_id, name="",
                               roots=cat.roots, home=tmp_path)
    loaded = load_catalog(path, cfg)
    assert loaded is not None
    assert {p.root_id for p in loaded.photos} == {id_a, id_b}
    assert [p.root_id for p in loaded.photos] == [id_a, id_b]  # order preserved


def test_save_catalog_retrying_succeeds_after_transient_errors(
        library: Path, tmp_path: Path) -> None:
    """save_catalog_retrying retries on OSError and returns on first success."""
    import unittest.mock as mock

    cat = scan_library(library)
    path = tmp_path / "catalog.json"
    call_count = 0

    original = __import__("catalog").save_catalog

    def flaky_save(catalog, p):
        nonlocal call_count
        call_count += 1
        if call_count < 3:  # fail twice, succeed on third
            raise OSError("transient sharing violation")
        original(catalog, p)

    with mock.patch("catalog.save_catalog", side_effect=flaky_save):
        # backoff=0 avoids real sleeps in tests
        save_catalog_retrying(cat, path, attempts=5, backoff=0)

    assert call_count == 3
    assert path.exists()
    assert load_catalog(path, library) is not None


def test_save_catalog_retrying_raises_after_all_attempts_exhausted(
        library: Path, tmp_path: Path) -> None:
    """save_catalog_retrying raises OSError when every attempt fails."""
    import unittest.mock as mock

    cat = scan_library(library)
    path = tmp_path / "catalog.json"
    sentinel = OSError("always broken")

    with mock.patch("catalog.save_catalog", side_effect=sentinel):
        with pytest.raises(OSError, match="always broken"):
            save_catalog_retrying(cat, path, attempts=3, backoff=0)

    assert not path.exists()  # nothing written on total failure


def test_reconcile_detects_drift(library: Path, tmp_path: Path) -> None:
    cat = scan_library(library)
    thumbcache.build_cache(cat, tmp_path / "c")  # fills size/mtime signals

    assert not reconcile_walk(cat, library).changed  # nothing changed yet

    make_jpeg(library / "2021-05-05 Picnic" / "new.jpg")  # add one
    (library / "2020-01-01 Trip" / "a.jpg").unlink()       # remove one
    drift = reconcile_walk(cat, library)
    assert drift.changed and drift.added == 1 and drift.removed == 1


def test_reconcile_walk_cancels(library: Path) -> None:
    import threading
    cat = scan_library(library)
    ev = threading.Event()
    ev.set()  # already cancelled
    assert reconcile_walk(cat, library, cancel=ev) is None


def test_reconcile_detects_ini_drift(library: Path, tmp_path: Path) -> None:
    """Externally editing a folder's .picasa.ini between scans sets
    Drift.ini_changed (fauxcasa-cam.14 step 4) even though no PHOTO file's
    own size/mtime changed — before this signal, a caption/keyword/star
    baked into the catalog at scan time would never refresh on a warm
    start until some unrelated photo drift happened to force a rebuild.
    Covers all three edit shapes: modified, newly added, and removed."""
    cat = scan_library(library)
    thumbcache.build_cache(cat, tmp_path / "c")
    assert not reconcile_walk(cat, library).changed  # baseline: clean

    # Modified: the Trip folder's existing ini gains a line (size+mtime
    # both change; a photo-only diff would miss this entirely).
    ini = library / "2020-01-01 Trip" / ".picasa.ini"
    ini.write_text(ini.read_text() + "caption=edited externally\r\n")
    drift = reconcile_walk(cat, library)
    assert drift.ini_changed and drift.changed
    assert drift.added == 0 and drift.removed == 0 and drift.modified == 0

    # Added: the Picnic folder in the `library` fixture has no ini at all
    # yet — a brand-new one appearing must be caught too, not just an
    # edit to an ini the catalog already knew about.
    cat2 = scan_library(library)  # fresh baseline, post-Trip-edit
    thumbcache.build_cache(cat2, tmp_path / "c2")
    assert not reconcile_walk(cat2, library).changed
    (library / "2021-05-05 Picnic" / ".picasa.ini").write_text(
        "[Picasa]\r\nname=Picnic!\r\n")
    assert reconcile_walk(cat2, library).ini_changed

    # Removed: deleting an ini the catalog DOES have a signal for.
    cat3 = scan_library(library)  # fresh baseline, post-Picnic-added
    thumbcache.build_cache(cat3, tmp_path / "c3")
    assert not reconcile_walk(cat3, library).changed
    ini.unlink()
    assert reconcile_walk(cat3, library).ini_changed


def test_reconcile_clean_after_warm_load_no_ini_changes(
        library: Path, tmp_path: Path) -> None:
    """A genuinely unchanged library reconciles clean end-to-end through a
    full save_catalog/load_catalog round trip (fauxcasa-cam.14 step 4):
    ini_sigs and contacts_sig persist and reload exactly, so a warm start
    with no external edits never spuriously flags Drift.ini_changed."""
    from catalog import stat_sig

    cat = scan_library(library)
    thumbcache.build_cache(cat, tmp_path / "c")
    cat.contacts_sig = stat_sig(
        _write_faces_contacts_xml(tmp_path / "contacts.xml"))
    path = tmp_path / "catalog.json"
    save_catalog(cat, path)

    loaded = load_catalog(path, library)
    assert loaded is not None
    assert loaded.ini_sigs == cat.ini_sigs
    assert loaded.ini_sigs  # the fixture's Trip folder does carry an ini
    assert loaded.contacts_sig == cat.contacts_sig

    drift = reconcile_walk(loaded, library)
    assert not drift.changed and not drift.ini_changed


def test_reconcile_detects_ancestor_root_ini_drift(
        library: Path, tmp_path: Path) -> None:
    """Codex review finding 2 (fauxcasa-cam.14 step 4): a .picasa.ini newly
    created in an ANCESTOR folder that owns no photo directly — including
    the bare library root — must set Drift.ini_changed even though
    reconcile_walk's fresh-side folder_rels (derived from walked PHOTO
    files) never puts "" in that set on its own. `catalog.ini_sigs` has
    no prior entry for "" either (the `library` fixture's root carries no
    ini and no photo lives directly at the root — every photo is under
    "2020-01-01 Trip" or "2021-05-05 Picnic"), so before the ancestor-
    closure fix this drift was invisible: neither side of the union ever
    contained the root."""
    cat = scan_library(library)
    thumbcache.build_cache(cat, tmp_path / "c")
    assert ("", "") not in cat.ini_sigs   # baseline: no root-level ini yet
    assert not reconcile_walk(cat, library).changed

    (library / ".picasa.ini").write_text("[Picasa]\r\nname=Library!\r\n")
    drift = reconcile_walk(cat, library)
    assert drift.ini_changed and drift.changed
    assert drift.added == 0 and drift.removed == 0 and drift.modified == 0

    # And the ini this reconcile just discovered is now correctly tracked
    # by a fresh scan — an ancestor-only ini registers in ini_sigs (§
    # folder_contacts' inheritance walk), same as any photo-owning one.
    cat2 = scan_library(library)
    assert ("", "") in cat2.ini_sigs


def test_set_data_invalidates_tiles() -> None:
    """The reconcile swap goes through grid.set_data; it must invalidate
    the decoded-tile cache or a rebuilt catalog paints stale thumbnails
    keyed by old indices (the major review finding)."""
    import os
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication
    from catalog import Catalog, Folder, Photo
    from grid import GridView

    app = QApplication.instance() or QApplication([])
    assert app is not None
    grid = GridView()
    cat = Catalog(root=Path("/x"),
                  photos=[Photo(rel="a.jpg", folder="", name="a.jpg")],
                  folders={"": Folder(rel="", title="x", photo_count=1)},
                  albums={})
    grid.set_data(cat, None)
    # simulate a populated tile cache + an in-flight decode generation
    grid.tiles[0] = [object(), 1, 999]
    grid._cache_bytes = 999
    grid.pending.add(5)
    gen_before = grid.generation
    grid.set_data(cat, None)  # the reconcile-style swap
    assert grid.tiles == {} and grid.pending == set()
    assert grid._cache_bytes == 0  # byte accounting resets with the cache
    assert grid.generation > gen_before  # stale queued decodes are dropped


def test_evict_bounds_by_bytes(monkeypatch) -> None:
    """Decoded-tile eviction is bounded by summed BYTES (oldest-first),
    never drops a tile the current paint wants, and an entry backstop
    bounds zero-byte (error) tiles that exert no byte pressure."""
    import os
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtGui import QColor, QImage
    from PySide6.QtWidgets import QApplication
    import grid as gridmod
    from grid import GridView

    app = QApplication.instance() or QApplication([])
    assert app is not None
    g = GridView()

    def tile(px: int) -> QImage:
        im = QImage(px, px, QImage.Format.Format_RGB32)
        im.fill(QColor(10, 20, 30))
        return im

    one = tile(64).sizeInBytes()  # 64*64*4 = 16384 B
    assert one > 0

    def fill(n: int) -> None:
        g.tiles.clear()
        g._cache_bytes = 0
        for i in range(n):
            img = tile(64)
            g.tiles[i] = [img, i, img.sizeInBytes()]  # frame_no i (i = newest)
            g._cache_bytes += img.sizeInBytes()

    # byte budget binds: room for 3 tiles, 6 present, none wanted ->
    # the three NEWEST survive, accounting stays exact
    monkeypatch.setattr(gridmod, "CACHE_BYTES", 3 * one)
    monkeypatch.setattr(gridmod, "CACHE_MAX_ENTRIES", 10_000)
    fill(6)
    g.wanted = frozenset()
    g._evict()
    assert set(g.tiles) == {3, 4, 5}
    assert g._cache_bytes == 3 * one == sum(g.tiles[i][2] for i in g.tiles)

    # wanted tiles are never evicted, even the oldest, even over budget
    fill(6)
    g.wanted = frozenset({0, 1})
    g._evict()
    assert {0, 1} <= set(g.tiles)
    assert g._cache_bytes <= 3 * one

    # entry backstop: zero-byte error tiles exert no byte pressure but an
    # all-error library still must not grow the dict without bound
    monkeypatch.setattr(gridmod, "CACHE_MAX_ENTRIES", 150)
    g.tiles.clear()
    g._cache_bytes = 0
    for i in range(300):
        g.tiles[i] = [None, i, 0]
    g.wanted = frozenset()
    g._evict()
    assert len(g.tiles) <= 150


def test_evict_wantband_over_budget_and_entry_floor(monkeypatch) -> None:
    """The load-bearing _evict invariant the byte-bound test doesn't reach:
    tiles the current paint WANTS are never evicted, even when the want-band
    alone blows past every bound, and the entry cap floors at the want-band
    size. Without these the eviction loop livelocks (re-evicting the very
    tiles the next paint re-requests) or over-evicts the visible set.

    * want-band over CACHE_BYTES: a want-band whose summed bytes EXCEED the
      byte budget must keep ALL of its tiles — _evict terminates with the
      whole set intact (no infinite loop chasing an unreachable budget, no
      eviction of a wanted tile). This is the single case that separates the
      correct impl from a livelock / over-eviction regression.
    * entry-cap floor: entry_cap = max(CACHE_MAX_ENTRIES, len(wanted)+128);
      when len(wanted)+128 is the larger (binding) term — never exercised by
      the other tests — the cache trims to THAT floor, not to the smaller
      CACHE_MAX_ENTRIES, so a large want-band can't be re-evicted to a tiny
      entry cap every frame.
    """
    import os
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtGui import QColor, QImage
    from PySide6.QtWidgets import QApplication
    import grid as gridmod
    from grid import GridView

    app = QApplication.instance() or QApplication([])
    assert app is not None
    g = GridView()

    def tile(px: int) -> QImage:
        im = QImage(px, px, QImage.Format.Format_RGB32)
        im.fill(QColor(10, 20, 30))
        return im

    one = tile(64).sizeInBytes()  # 64*64*4 = 16384 B
    assert one > 0

    # --- want-band over budget: every tile present is wanted and together
    # they exceed CACHE_BYTES. _evict must keep them all and return; the
    # by-age list excludes wanted tiles, so there is nothing to evict and no
    # spin. A regression that evicts wanted tiles, or loops forever chasing
    # the byte budget, fails here (a livelock would hang the test).
    monkeypatch.setattr(gridmod, "CACHE_BYTES", 3 * one)       # room for 3
    monkeypatch.setattr(gridmod, "CACHE_MAX_ENTRIES", 10_000)  # never binds
    g.tiles.clear()
    g._cache_bytes = 0
    for i in range(6):  # 6 * one > 3 * one = CACHE_BYTES
        img = tile(64)
        g.tiles[i] = [img, i, img.sizeInBytes()]
        g._cache_bytes += img.sizeInBytes()
    g.wanted = frozenset(range(6))  # the whole over-budget set is wanted
    g._evict()
    assert set(g.tiles) == set(range(6))          # nothing evicted
    assert g._cache_bytes == 6 * one              # accounting exact, intact
    assert g._cache_bytes > gridmod.CACHE_BYTES   # really was over budget

    # --- entry-cap floor binds: len(wanted)+128 (133) > CACHE_MAX_ENTRIES
    # (10), so the floor is the cap. Zero-byte error tiles exert no byte
    # pressure, isolating the entry path; eviction trims NON-wanted tiles
    # oldest-first down to the floor while keeping every wanted tile. A
    # regression that dropped the floor would trim to CACHE_MAX_ENTRIES (10)
    # instead, evicting wanted tiles the paint still needs.
    monkeypatch.setattr(gridmod, "CACHE_BYTES", 1 << 30)  # bytes never bind
    monkeypatch.setattr(gridmod, "CACHE_MAX_ENTRIES", 10)
    wanted = set(range(5))
    floor = len(wanted) + 128  # 133 — the binding term
    assert floor > gridmod.CACHE_MAX_ENTRIES
    g.tiles.clear()
    g._cache_bytes = 0
    for i in range(200):  # 5 wanted + 195 non-wanted, all 0-byte error tiles
        g.tiles[i] = [None, i, 0]  # frame_no i (i = newest)
    g.wanted = frozenset(wanted)
    g._evict()
    assert len(g.tiles) == floor   # trimmed to the floor, NOT to 10
    assert wanted <= set(g.tiles)  # every wanted tile survives
    # survivors beyond the want-band are the NEWEST non-wanted tiles
    # (oldest-first eviction): indices 0..4 wanted + 72..199 non-wanted
    assert max(g.tiles) == 199 and min(g.tiles) == 0


def test_prefetch_margin_caps_band_to_byte_budget() -> None:
    """The want-band cap (fauxcasa-q6l.14): prefetch_margin is a pure
    function that shrinks the prefetch margin so the whole band fits the
    byte budget in native tile bytes — full PREFETCH_SCREENS on a normal
    viewport, monotonically shrinking as the tile count grows, 0 once the
    visible set alone exceeds the budget (visible is never dropped; only
    prefetch gives way)."""
    from grid import PREFETCH_SCREENS, prefetch_margin

    native = 256 * 256 * 4  # dpr-1 native tile bytes (256 KB)

    # normal viewport at default zoom (160 px tiles): the band fits with
    # room to spare -> the full PREFETCH_SCREENS margin, unchanged behavior
    full = int(800 * PREFETCH_SCREENS)
    assert prefetch_margin(1280, 800, 160, native) == full

    # Regime assertions use an EXPLICIT band_budget so they pin the pure
    # function's behavior at a known point, not today's module CACHE_BYTES
    # (raising the cache budget must not fail this test).
    budget = 1024 * native  # ~1024-tile budget (256 MiB at dpr 1)

    # margin never grows as tiles shrink (more visible per screen)
    margins = [prefetch_margin(3840, 2160, t, native, band_budget=budget)
               for t in (256, 192, 128, 96, 64)]
    assert all(a >= b for a, b in zip(margins, margins[1:]))
    assert margins[0] == int(2160 * PREFETCH_SCREENS)  # roomy at max zoom

    # 4K fullscreen at min zoom: the visible set alone (~1700 cells backed
    # by 256 KB native tiles) exceeds the 1024-tile budget -> margin 0
    assert prefetch_margin(3840, 2160, 64, native, band_budget=budget) == 0

    # explicit budget: visible alone over a tiny budget -> 0; a huge
    # budget -> the full margin again
    assert prefetch_margin(1280, 800, 64, native, band_budget=native * 4) == 0
    assert prefetch_margin(1280, 800, 64, native, band_budget=1 << 40) == full


def test_scaled_paint_cache_reuses_and_invalidates(tmp_path: Path) -> None:
    """The scaled-paint cache (fauxcasa-q6l.14): painting below native tile
    size smooth-scales each tile ONCE and caches the result in the entry
    (slots 3/4); the next frame blits the SAME image object with no
    transform work. A zoom change drops every variant (they are keyed to
    device-pixel size) and releases their bytes; the variant's bytes are folded
    into the entry's nbytes slot and _cache_bytes so eviction stays honest.
    At max zoom (tile == native) no variant is ever created."""
    from PySide6.QtGui import QColor, QImage

    g = _selection_grid(tmp_path)
    native_nbytes = 256 * 256 * 4

    for i in g.display:  # inject decoded native-resolution tiles
        img = QImage(256, 256, QImage.Format.Format_RGB32)
        img.fill(QColor(40 + i * 8, 80, 120))
        assert img.sizeInBytes() == native_nbytes
        g.tiles[i] = [img, 0, native_nbytes, None, 0]
        g._cache_bytes += native_nbytes
    native_sum = g._cache_bytes

    g.set_zoom(64)                        # min zoom: all 9 tiles visible
    assert not g.grab().isNull()          # first paint: scales + caches
    t0 = g.tiles[g.display[0]]
    assert t0[3] is not None and t0[4] == (64, 64)  # keyed by device size
    assert t0[3].width() == 64            # offscreen dpr 1 -> 64 px variant
    scaled0 = t0[3]
    # bytes accounting includes the scaled variant, exactly
    assert t0[2] == native_nbytes + scaled0.sizeInBytes()
    assert g._cache_bytes == sum(t[2] for t in g.tiles.values())
    assert g._cache_bytes > native_sum

    g.grab()                              # second paint: pure reuse
    assert g.tiles[g.display[0]][3] is scaled0  # same object, no re-scale

    g.set_zoom(128)                       # zoom change invalidates variants
    assert all(t[3] is None and t[4] == 0 for t in g.tiles.values())
    assert g._cache_bytes == native_sum   # scaled bytes released
    g.grab()                              # re-caches at the new zoom
    t0 = g.tiles[g.display[0]]
    assert t0[3] is not None and t0[4] == (128, 128)
    assert t0[3].width() == 128

    g.set_zoom(256)                       # max zoom == native size
    g.grab()
    assert all(t[3] is None for t in g.tiles.values())  # guard: no variants
    assert g._cache_bytes == native_sum


def test_scaled_paint_cache_skips_marginal_downscale(tmp_path: Path) -> None:
    """Regression for the hi-DPI rounding duplicate (q6l.14 review): the
    paint cache must only cache a MEANINGFUL downscale (>= 2x area
    reduction). At dpr 2 and max zoom a landscape 512x341 native tile
    aspect-fits to 256x170 logical -> 512x340 DEVICE px: a 1-px rounding
    artifact, not a downscale. The old `sw < width or sh < height` gate
    cached a near-full-resolution duplicate of every such tile (doubling
    _cache_bytes and halving the effective tile cache); the area gate draws
    the native image directly and caches nothing. A real downscale (min
    zoom) still caches, keyed by device-pixel size."""
    from PySide6.QtGui import QColor, QImage

    g = _selection_grid(tmp_path)
    g.devicePixelRatioF = lambda: 2.0     # hi-DPI paint path
    g.grab()                              # _refresh_tile_native picks it up
    assert g._tile_native == 512

    for i in g.display:  # inject landscape native tiles (aspect != 1)
        img = QImage(512, 341, QImage.Format.Format_RGB32)
        img.fill(QColor(40 + i * 8, 80, 120))
        g.tiles[i] = [img, 0, img.sizeInBytes(), None, 0]
        g._cache_bytes += img.sizeInBytes()
    native_sum = g._cache_bytes

    g.set_zoom(256)                       # max zoom
    g.grab()                              # sw=512, sh=340: rounding only
    assert all(t[3] is None for t in g.tiles.values())  # nothing cached
    assert g._cache_bytes == native_sum   # no duplicated bytes charged

    g.set_zoom(64)                        # min zoom: a REAL downscale
    g.grab()                              # 64x43 logical -> 128x86 device
    t0 = g.tiles[g.display[0]]
    assert t0[3] is not None
    assert t0[4] == (t0[3].width(), t0[3].height()) == (128, 86)
    assert g._cache_bytes == sum(t[2] for t in g.tiles.values())
    assert g._cache_bytes > native_sum


def test_wanted_band_capped_on_huge_viewport() -> None:
    """4K fullscreen at min zoom (fauxcasa-q6l.14): once the visible set
    alone exceeds the byte budget the published want-band is EXACTLY the
    visible set (margin 0) — prefetch no longer amplifies the extreme
    viewport into a 1 GB never-evicted band."""
    _offscreen_app()
    from catalog import Catalog, Folder, Photo
    from grid import GridView, prefetch_margin

    n = 4000
    cat = Catalog(root=Path("/x"),
                  photos=[Photo(rel=f"p{i:04d}.jpg", folder="",
                                name=f"p{i:04d}.jpg") for i in range(n)],
                  folders={"": Folder(rel="", title="x", photo_count=n)},
                  albums={})
    g = GridView()
    g.resize(3840, 2160)          # simulated 4K fullscreen
    g.show()                      # hidden widgets keep a stale viewport size
    g.set_data(cat, None)         # no thumbs: band math only, no decodes
    g.set_zoom(64)                # min zoom
    g.grab()                      # paints -> publishes self.wanted

    vp = g.viewport()
    native = g._tile_native * g._tile_native * 4
    assert prefetch_margin(vp.width(), vp.height(), g.tile, native) == 0
    top = g.verticalScrollBar().value()
    visible = sum(1 for _ in g._visible_items(top, top + vp.height()))
    assert visible > 1000                 # really is an extreme viewport
    assert len(g.wanted) == visible       # margin 0: wanted == visible


def test_load_catalog_survives_malformed_rows(library: Path,
                                               tmp_path: Path) -> None:
    cat = scan_library(library)
    thumbcache.build_cache(cat, tmp_path / "c")
    path = tmp_path / "catalog.json"
    save_catalog(cat, path)
    data = _raw_catalog(path)
    # A structurally broken FIRST GROUP (not even a dict) — ungrouping it
    # must raise (AttributeError), which load_catalog degrades to None,
    # exactly like a broken flat row did pre-ed5.5.
    data["photos"][0] = "not_a_group"
    _write_raw_catalog(path, data)
    assert load_catalog(path, library) is None  # -> caller cold-walks


def test_load_rejects_old_sidecar(library: Path, tmp_path: Path) -> None:
    result = thumbcache.build_cache(cat := scan_library(library), tmp_path / "c")
    sidecar = result.path.with_suffix(".fcache.json")
    meta = json.loads(sidecar.read_text())
    del meta["files"]
    sidecar.write_text(json.dumps(meta))
    with pytest.raises(thumbcache.CacheError, match="files"):
        thumbcache.load_cache(result.path)
    assert cat  # silence linters


def test_load_rejects_corrupt(tmp_path: Path) -> None:
    bad = tmp_path / "bad.fcache"
    bad.write_bytes(b"NOPE" + b"\x00" * 12)
    with pytest.raises(thumbcache.CacheError):
        thumbcache.load_cache(bad)
    short = tmp_path / "short.fcache"
    short.write_bytes(thumbcache.MAGIC + struct.pack("<III", 1, 99, 0))
    with pytest.raises(thumbcache.CacheError, match="truncated"):
        thumbcache.load_cache(short)


def test_unreadable_image_gets_error_entry(tmp_path: Path) -> None:
    root = tmp_path / "lib"
    make_jpeg(root / "f" / "ok.jpg")
    (root / "f" / "corrupt.jpg").write_bytes(b"not a jpeg at all")
    cat = scan_library(root)
    result = thumbcache.build_cache(cat, tmp_path / "c")
    cache = thumbcache.load_cache(result.path)
    by_rel = dict(zip(cache.files, cache.entries))
    assert by_rel["f/corrupt.jpg"][1] == 0  # zero-length = error tile
    assert by_rel["f/ok.jpg"][1] > 0
