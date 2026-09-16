"""Tests for library.py multi-root journeys (promote_library, add-root/--promote CLI, Picasa watched-folders import).

Split from test_tracer.py (fauxcasa-l09); originally lines 16911-17391 of the monolith."""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path
import pytest
import library as libmod
import thumbcache
from catalog import (
    load_catalog,
    reconcile_walk,
    save_catalog,
    scan_library,
)
from tracer_helpers import (
    APP_DIR,
    _promote_fixture,
    _raw_catalog,
    _raw_photo_rows,
    make_jpeg,
)


# ===========================================================================
# ---- multi-root journeys (fauxcasa-ed5.7.4, bead .d): promote_library(),
# ---- add-root / --promote CLI wiring, Picasa watched-folders import
# ===========================================================================


def test_promote_happy_path(tmp_path: Path) -> None:
    """promote_library (design §10): library.json exists carrying the
    minted identity, the cache dir is renamed from the legacy path-digest
    to the library_id digest (the OLD digest is gone), thumbs.fcache/.json
    are renamed to the per-root suffixed names, and catalog.json is
    upgraded in place — version bumped, library_id + roots header, a .bak
    kept — with photo rows untouched."""
    import catalog as catmod

    root, cache_root, cat, old_dir = _promote_fixture(tmp_path)
    assert old_dir.is_dir()

    cfg = libmod.promote_library(root, cache_root=cache_root)

    assert not cfg.is_legacy
    assert cfg.home == root.resolve()
    json_path = libmod.library_json_path(cfg.home)
    assert json_path.is_file()
    raw = json.loads(json_path.read_text(encoding="utf-8"))
    assert raw["library_id"] == cfg.library_id
    assert len(raw["roots"]) == 1
    root_id = raw["roots"][0]["id"]
    assert re.fullmatch(r"[0-9a-f]{8}", root_id)
    assert libmod.read_root_marker(root) == root_id

    new_dir = thumbcache.cache_dir_for(cfg.library_id, cache_root)
    assert new_dir.is_dir()
    assert not old_dir.is_dir(), "old digest cache dir must be GONE (renamed)"

    assert (new_dir / f"thumbs-{root_id}.fcache").is_file()
    assert (new_dir / f"thumbs-{root_id}.fcache.json").is_file()
    assert not (new_dir / "thumbs.fcache").exists()
    assert not (new_dir / "thumbs.fcache.json").exists()

    cat_data = _raw_catalog(new_dir / "catalog.json")
    assert cat_data["version"] == catmod.CATALOG_VERSION
    assert cat_data["library_id"] == cfg.library_id
    assert cat_data["roots"] == [{"id": root_id, "path": str(root.resolve())}]
    assert "library" not in cat_data
    assert (new_dir / "catalog.json.bak").is_file()

    # bind() still works: the fcache's own content (files[]) is untouched
    # by the rename, so it still binds against the promoted catalog's slice
    loaded_cache = thumbcache.load_cache(new_dir / f"thumbs-{root_id}.fcache")
    loaded_cat = load_catalog(new_dir / "catalog.json", cfg)
    assert loaded_cat is not None
    thumbcache.bind(loaded_cache, loaded_cat, root_id=root_id)


def test_promote_rekeys_ini_sigs_to_minted_root_id(tmp_path: Path) -> None:
    """Codex review finding 1 (fauxcasa-cam.14 step 4): ini_sigs is nested
    by root_id and does NOT use the absent-means-roots[0] convention
    photo/folder rows do (save_catalog's comment), so a legacy catalog's
    ini signatures — persisted under the literal LEGACY_ROOT_ID ("") key —
    must be REKEYED onto the minted root_id during promotion, or every
    existing ini reads as newly added on the first warm reconcile (a
    100%-miss lookup against the new root_id) and forces a spurious full
    reindex."""
    root = tmp_path / "lib"
    make_jpeg(root / "a.jpg")
    (root / ".picasa.ini").write_text("[Picasa]\r\nname=Lib\r\n")
    cache_root = tmp_path / "cache"
    cat = scan_library(root)
    assert cat.ini_sigs  # baseline: the fixture's root ini WAS captured
    old_dir = thumbcache.cache_dir_for(str(root.resolve()), cache_root)
    thumbcache.build_cache(cat, old_dir)
    save_catalog(cat, old_dir / "catalog.json")

    raw_before = _raw_catalog(old_dir / "catalog.json")
    assert list(raw_before["ini_sigs"]) == [""]  # legacy: LEGACY_ROOT_ID key

    cfg = libmod.promote_library(root, cache_root=cache_root)
    new_dir = thumbcache.cache_dir_for(cfg.library_id, cache_root)
    root_id = cfg.roots[0].id

    raw_after = _raw_catalog(new_dir / "catalog.json")
    assert list(raw_after["ini_sigs"]) == [root_id]  # rekeyed, not "" anymore

    loaded = load_catalog(new_dir / "catalog.json", cfg)
    assert loaded is not None
    assert loaded.ini_sigs == {(root_id, ""): cat.ini_sigs[("", "")]}

    # THE regression: a clean warm reconcile right after promotion must
    # not see the pre-existing ini as newly added.
    drift = reconcile_walk(loaded, root, root_id=root_id)
    assert not drift.ini_changed


def test_promoted_rows_equal_old_rows(tmp_path: Path) -> None:
    """The bead's named acceptance test (design §10's "promoted-file-
    equals-old-file" property): a synthetic catalog fixture (real shape,
    synthetic content, per privacy rules) is promoted and the resulting
    photo ROWS are byte-identical to the input rows — promotion rewrites
    ONLY the header; sha256 backfill and every other per-row field
    (star/caption/keywords included) survive untouched."""
    root = tmp_path / "lib"
    make_jpeg(root / "2020" / "a.jpg")
    make_jpeg(root / "b.jpg")
    cache_root = tmp_path / "cache"

    cat = scan_library(root)
    old_dir = thumbcache.cache_dir_for(str(root.resolve()), cache_root)
    result = thumbcache.build_cache(cat, old_dir)  # fills size/mtime/sha256
    assert result is not None
    # Non-default fields widen the byte-identity check past bare defaults.
    cat.photos[0].star = 3
    cat.photos[0].caption = "hello"
    cat.photos[0].keywords = ("x", "y")
    save_catalog(cat, old_dir / "catalog.json")

    before_raw = _raw_catalog(old_dir / "catalog.json")
    before = before_raw["photos"]
    assert all(p["x"] for row in before for p in row["ph"]), \
        "sha256 backfill must be present pre-promotion"

    cfg = libmod.promote_library(root, cache_root=cache_root)
    new_dir = thumbcache.cache_dir_for(cfg.library_id, cache_root)
    after = _raw_catalog(new_dir / "catalog.json")["photos"]

    assert after == before  # the grouped "photos" array itself, untouched


def test_promote_upgrades_legacy_plain_json_catalog(tmp_path: Path) -> None:
    """_promote_upgrade_catalog's other input shape (fauxcasa-ed5.5): an
    on-disk catalog.json from BEFORE the zstd/grouped bump — plain JSON,
    flat rows, no "R" — is not just header-rewritten but fully regrouped
    and compressed into the current v13 format; the promoted file loads
    cleanly and its rows carry the exact same content as the pre-upgrade
    flat rows, just reshaped."""
    import catalog as catmod

    root = tmp_path / "lib"
    make_jpeg(root / "2020" / "a.jpg")
    make_jpeg(root / "b.jpg")
    cache_root = tmp_path / "cache"
    old_dir = thumbcache.cache_dir_for(str(root.resolve()), cache_root)
    old_dir.mkdir(parents=True)

    flat_rows = [
        {"r": "2020/a.jpg", "s": 2, "c": "hi", "z": 10, "m": 100,
         "x": "ab" * 32},
        {"r": "b.jpg", "z": 20, "m": 200, "x": "cd" * 32},
    ]
    legacy_data = {
        "version": catmod.PRE_MULTIROOT_VERSION,
        "library": str(root.resolve()),
        "photos": flat_rows,
        "folders": {}, "hidden_folders": [], "contacts": {},
        "db3_contacts": [], "albums": [],
    }
    (old_dir / "catalog.json").write_text(json.dumps(legacy_data))

    cfg = libmod.promote_library(root, cache_root=cache_root)
    new_dir = thumbcache.cache_dir_for(cfg.library_id, cache_root)
    cat_path = new_dir / "catalog.json"

    raw = cat_path.read_bytes()
    assert raw[:4] == catmod.ZSTD_MAGIC        # now compressed, not plain
    data = _raw_catalog(cat_path)
    assert data["version"] == catmod.CATALOG_VERSION
    assert data["library_id"] == cfg.library_id
    # regrouped, not left flat: every group carries "ph", never a bare "r"
    assert all("ph" in g for g in data["photos"])
    assert _raw_photo_rows(data) == flat_rows   # same content, reshaped only

    loaded = load_catalog(cat_path, cfg)
    assert loaded is not None
    a = next(p for p in loaded.photos if p.rel == "2020/a.jpg")
    assert a.star == 2 and a.caption == "hi" and a.sha256 == "ab" * 32


def test_promote_regroups_noncontiguous_legacy_rows(tmp_path: Path) -> None:
    """Regression for the same non-contiguous-folder hazard as
    test_group_photo_rows_preserves_order_when_folder_split_by_subfolder,
    but through _promote_upgrade_catalog's regrouping path (library.py):
    a legacy flat catalog whose "2020" folder is split by a "2020/summer"
    subfolder must regroup to the EXACT same flattened row order, not a
    dict-insertion-order permutation."""
    import catalog as catmod

    root = tmp_path / "lib"
    make_jpeg(root / "2020" / "apple.jpg")
    make_jpeg(root / "2020" / "summer" / "b.jpg")
    make_jpeg(root / "2020" / "zoo.jpg")
    cache_root = tmp_path / "cache"
    old_dir = thumbcache.cache_dir_for(str(root.resolve()), cache_root)
    old_dir.mkdir(parents=True)

    flat_rows = [
        {"r": "2020/apple.jpg", "z": 10, "m": 100, "x": "ab" * 32},
        {"r": "2020/summer/b.jpg", "z": 20, "m": 200, "x": "cd" * 32},
        {"r": "2020/zoo.jpg", "z": 30, "m": 300, "x": "ef" * 32},
    ]
    legacy_data = {
        "version": catmod.PRE_MULTIROOT_VERSION,
        "library": str(root.resolve()),
        "photos": flat_rows,
        "folders": {}, "hidden_folders": [], "contacts": {},
        "db3_contacts": [], "albums": [],
    }
    (old_dir / "catalog.json").write_text(json.dumps(legacy_data))

    cfg = libmod.promote_library(root, cache_root=cache_root)
    new_dir = thumbcache.cache_dir_for(cfg.library_id, cache_root)
    data = _raw_catalog(new_dir / "catalog.json")
    assert _raw_photo_rows(data) == flat_rows  # exact order preserved


def test_promote_rollback_on_injected_failure(tmp_path: Path,
                                              monkeypatch) -> None:
    """A step monkeypatched to raise mid-promotion rolls EVERYTHING back:
    the cache dir is renamed back to the legacy digest, thumbs.fcache/.json
    are un-suffixed again, catalog.json is left exactly as it was (no
    .bak left dangling), no partial .fauxcasa/ remains — and legacy open
    still works (design §10 point 4's reversal contract, enforced from
    inside promote_library itself, not left to the caller)."""
    import catalog as catmod

    root, cache_root, cat, old_dir = _promote_fixture(tmp_path)

    def boom(*a, **kw):
        raise RuntimeError("injected failure")
    monkeypatch.setattr(libmod, "_promote_upgrade_catalog", boom)

    with pytest.raises(RuntimeError, match="injected failure"):
        libmod.promote_library(root, cache_root=cache_root)

    assert not (root / ".fauxcasa").exists()
    assert old_dir.is_dir()
    assert (old_dir / "thumbs.fcache").is_file()
    assert (old_dir / "thumbs.fcache.json").is_file()
    assert (old_dir / "catalog.json").is_file()
    assert not (old_dir / "catalog.json.bak").exists()
    assert not (old_dir / "catalog.json.tmp").exists()

    loaded = catmod.load_catalog(old_dir / "catalog.json", root)
    assert loaded is not None
    assert len(loaded.photos) == 1


def test_promote_reversal_delete_fauxcasa_restores_legacy(
        tmp_path: Path) -> None:
    """Reversal contract (design §10 point 4): after a SUCCESSFUL
    promotion, deleting .fauxcasa/ makes resolve_open_path fall back to
    implicit-legacy — worst case a cold walk (N3), never an error — the
    user-facing "just delete .fauxcasa/" instruction."""
    import shutil as _shutil

    root, cache_root, cat, old_dir = _promote_fixture(tmp_path)
    libmod.promote_library(root, cache_root=cache_root)
    assert (root / ".fauxcasa").exists()

    _shutil.rmtree(root / ".fauxcasa")

    reopened = libmod.resolve_open_path(root)
    assert reopened.is_legacy

    # worst case: a fresh cold walk still works fine
    cat2 = scan_library(root)
    assert len(cat2.photos) == 1


def test_import_picasa_watched_builds_n_root_config(tmp_path: Path) -> None:
    """import_picasa_watched (design §1 Journey B): one root per watched
    folder, in list order, fresh library-home, minted unique ids — the N
    watched folders become N roots with no re-walk beyond the fresh scan
    each root will naturally need on first open."""
    home = tmp_path / "home"
    wf_a, wf_b, wf_c = (tmp_path / n for n in ("wf-a", "wf-b", "wf-c"))
    for d in (wf_a, wf_b, wf_c):
        d.mkdir()

    cfg = libmod.import_picasa_watched([wf_a, wf_b, wf_c], home,
                                       name="Family")
    assert not cfg.is_legacy
    assert cfg.home == home
    assert cfg.name == "Family"
    assert len(cfg.roots) == 3
    assert [r.path for r in cfg.roots] == [wf_a, wf_b, wf_c]
    assert len({r.id for r in cfg.roots}) == 3  # all unique

    libmod.save_library(cfg)
    loaded = libmod.load_library(home)
    assert loaded is not None
    assert len(loaded.roots) == 3


def test_import_picasa_watched_rejects_nesting(tmp_path: Path) -> None:
    """A nested, duplicate, or nonexistent folder is SKIPPED (reported via
    the `skipped` out-param) rather than aborting the rest of the import —
    Picasa users routinely accumulate stale/overlapping watched entries."""
    home = tmp_path / "home"
    a = tmp_path / "a"
    a.mkdir()
    a_sub = a / "sub"
    a_sub.mkdir()
    missing = tmp_path / "does-not-exist"

    skipped: list[str] = []
    cfg = libmod.import_picasa_watched([a, a_sub, a, missing], home,
                                       skipped=skipped)
    assert len(cfg.roots) == 1
    assert cfg.roots[0].path == a
    assert len(skipped) == 3  # a_sub (nested), a (dup), missing (not a dir)


def test_picasa_watched_from_registry_seam(monkeypatch) -> None:
    """picasa_watched_from_registry() is monkeypatched at the
    _read_hotfolders_raw seam so tests never touch winreg itself: a
    present value parses into Paths (';'-separated), and an absent value
    raises a clear RuntimeError rather than silently returning zero
    roots (a user who explicitly asked for the registry source should
    hear why nothing came back)."""
    monkeypatch.setattr(
        libmod, "_read_hotfolders_raw",
        lambda: r"C:\Users\a\Pictures;C:\Users\a\Scans")
    folders = libmod.picasa_watched_from_registry()
    assert folders == [Path(r"C:\Users\a\Pictures"),
                       Path(r"C:\Users\a\Scans")]

    monkeypatch.setattr(libmod, "_read_hotfolders_raw", lambda: None)
    with pytest.raises(RuntimeError, match="registry"):
        libmod.picasa_watched_from_registry()


def test_read_hotfolders_raw_handles_reg_multi_sz_list(monkeypatch) -> None:
    """A REG_MULTI_SZ registry value arrives from winreg as a Python list,
    not a string — str()-coercing it would stringify to garbage like
    "['a', 'b']". _read_hotfolders_raw must join list/tuple values with a
    separator the downstream re.split([;|\\n]+) in
    picasa_watched_from_registry already tolerates. This mocks the
    registry read itself (winreg.OpenKey/QueryValueEx) rather than the
    _read_hotfolders_raw seam, so it exercises the list-handling branch
    directly; a fake winreg module is installed in sys.modules so the
    test runs on non-Windows too."""
    import contextlib
    import types

    fake_winreg = types.ModuleType("winreg")
    fake_winreg.HKEY_CURRENT_USER = object()

    @contextlib.contextmanager
    def fake_open_key(_hive, _path):
        yield object()

    fake_winreg.OpenKey = fake_open_key
    fake_winreg.QueryValueEx = lambda _key, _name: (
        [r"C:\Users\a\Pictures", r"C:\Users\a\Scans"], 7)  # REG_MULTI_SZ

    monkeypatch.setitem(sys.modules, "winreg", fake_winreg)
    raw = libmod._read_hotfolders_raw()
    assert raw == "C:\\Users\\a\\Pictures\nC:\\Users\\a\\Scans"
    # And the full parse still yields the right Paths downstream.
    folders = libmod.picasa_watched_from_registry()
    assert folders == [Path(r"C:\Users\a\Pictures"),
                       Path(r"C:\Users\a\Scans")]


def test_main_promote_and_add_root_cli(tmp_path: Path) -> None:
    """End-to-end --promote then --add-root via subprocess (bead .d): a
    legacy library is promoted in place, then a second root is added to
    the resulting home — both print a one-line summary and exit 0 without
    launching the normal open/grid path (real process — see
    test_main_bad_library_exits_2 for why these CLI actions run out of
    process rather than via a direct main() call)."""
    import os
    import subprocess

    main_py = APP_DIR / "main.py"
    env = dict(os.environ, QT_QPA_PLATFORM="offscreen")

    root = tmp_path / "lib"
    make_jpeg(root / "a.jpg")
    cache_root = tmp_path / "cache"
    root2 = tmp_path / "root2"
    make_jpeg(root2 / "b.jpg")

    # Cold-walk build first: promote_library expects an already-opened
    # library's cache dir to migrate (Journey A).
    proc = subprocess.run(
        [sys.executable, str(main_py), str(root),
         "--cache-root", str(cache_root),
         "--quit-after-ready", "--finish-build"],
        capture_output=True, text=True, env=env)
    assert proc.returncode == 0, (proc.returncode, proc.stdout, proc.stderr)

    proc = subprocess.run(
        [sys.executable, str(main_py), str(root),
         "--cache-root", str(cache_root), "--promote"],
        capture_output=True, text=True, env=env)
    assert proc.returncode == 0, (proc.returncode, proc.stdout, proc.stderr)
    assert "promoted: library home at" in proc.stdout

    proc = subprocess.run(
        [sys.executable, str(main_py), str(root),
         "--cache-root", str(cache_root), "--add-root", str(root2)],
        capture_output=True, text=True, env=env)
    assert proc.returncode == 0, (proc.returncode, proc.stdout, proc.stderr)
    assert "added root" in proc.stdout

    loaded = libmod.load_library(root)
    assert loaded is not None
    assert len(loaded.roots) == 2
    assert loaded.roots[1].path == root2.resolve()


def test_cmd_promote_requires_explicit_library(monkeypatch, tmp_path: Path,
                                               capsys) -> None:
    """--promote with NO positional library argument must error out (exit
    2) rather than falling through to _resolve_library's built-in-default
    fallback and promoting the SAMPLE library in place — the same explicit-
    target rule _cmd_import_picasa_watched already enforces. Monkeypatching
    library.promote_library to raise if called proves the fallthrough never
    even reaches the promotion step. Asserts on capsys' stderr mirror
    (applog's _StderrHandler) rather than caplog, matching the convention
    used elsewhere in this file for log.error() assertions — caplog's
    non-propagating-logger handler only attaches to loggers that already
    exist in logging's registry as of pytest_runtest_setup, so it can't
    reliably see "fauxcasa" (propagate=False) on a first-in-process run of
    just this test."""
    import main as mainmod

    def _boom(*_a, **_kw):
        raise AssertionError("promote_library must not run without an "
                             "explicit library argument")

    monkeypatch.setattr(mainmod.library, "promote_library", _boom)
    rc = mainmod._cmd_promote(None, tmp_path / "cache")
    assert rc == 2
    assert "--promote requires a library" in capsys.readouterr().err


def test_main_import_picasa_watched_listfile_cli(tmp_path: Path) -> None:
    """--import-picasa-watched LISTFILE via subprocess: creates a fresh
    library-home with one root per listed folder; '#' comments and blank
    lines are ignored."""
    import os
    import subprocess

    main_py = APP_DIR / "main.py"
    env = dict(os.environ, QT_QPA_PLATFORM="offscreen")

    home = tmp_path / "home"
    wf_a, wf_b = tmp_path / "wf-a", tmp_path / "wf-b"
    wf_a.mkdir()
    wf_b.mkdir()
    listfile = tmp_path / "watched.txt"
    listfile.write_text(f"# comment\n{wf_a}\n\n{wf_b}\n", encoding="utf-8")

    proc = subprocess.run(
        [sys.executable, str(main_py), str(home),
         "--cache-root", str(tmp_path / "cache"),
         "--import-picasa-watched", str(listfile)],
        capture_output=True, text=True, env=env)
    assert proc.returncode == 0, (proc.returncode, proc.stdout, proc.stderr)
    assert "imported 2 root(s)" in proc.stdout

    loaded = libmod.load_library(home)
    assert loaded is not None
    assert len(loaded.roots) == 2
