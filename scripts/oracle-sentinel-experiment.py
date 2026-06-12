#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.10"
# dependencies = []
# ///
"""Sentinel-acceptance experiment at the Wine oracle (fauxcasa-5kl).

Feeds variant repository.dat / usernames.dat files to real Picasa 3.9.141
and observes whether it accepts, rewrites, or rebuilds. Each variant runs in
a disposable clone of the live oracle prefix; the clone's ``dosdevices/z:``
is repointed at a private staging tree holding a copy of the synthetic
library, so the ``Z:\\...`` paths baked into db3 resolve to writable copies
and the experiment can NEVER touch the live oracle prefix, the live
synthetic library, or the real home directory (user-dir symlinks are
replaced with empty dirs too).

Picasa runs headless on a dedicated Xwayland display (weston
``--backend=headless --xwayland`` in the ``benchbox`` toolbox); window
screenshots are taken with xwd so error dialogs are observable.

Typical session::

    toolbox run -c benchbox weston --backend=headless --xwayland \\
        --width=1600 --height=1000 --socket=wayland-sent --idle-time=0 &
    uv run scripts/oracle-sentinel-experiment.py master   # quiescent clone
    uv run scripts/oracle-sentinel-experiment.py run      # all variants
    uv run scripts/oracle-sentinel-experiment.py report
    uv run scripts/oracle-sentinel-experiment.py clean    # rm clones

Everything lives under cache/ (gitignored); curated findings go to
docs/research/oracle-db3-survey.md by hand.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from picasa_db import encode_repository, read_repository  # noqa: E402

REPO = Path(__file__).resolve().parent.parent
CACHE = REPO / "cache"
LIVE_PREFIX = CACHE / "wine-oracle"
LIVE_LIBRARY = CACHE / "synthetic-library"
MASTER = CACHE / "sentinel-master"
MASTER_LIB = CACHE / "sentinel-master-library"
CLONES = CACHE / "sentinel-clones"
RUNS = CACHE / "sentinel-runs"

PICASA_EXE = r"C:\Program Files (x86)\Google\Picasa3\Picasa3.exe"
APPDATA_GOOGLE = "drive_c/users/matt/AppData/Local/Google"
USER_DIR_SYMLINKS = ["Desktop", "Documents", "Downloads", "Music", "Pictures", "Videos"]

DISPLAY = ":2"
TOOLBOX = "benchbox"
WATCH_SECS = 360
SETTLE_SECS = 20  # post-close window for an exit flush


# ------------------------------------------------------------------ variants

def build_variants(pairs: list[tuple[str, str]]) -> dict[str, dict]:
    """Variant name -> {repository: bytes|None|'absent', usernames: bytes|None}.

    ``None`` means leave the master's file untouched; ``'absent'`` deletes it.
    """

    def repl(key: str, val: str) -> list[tuple[str, str]]:
        return [(k, val if k == key else v) for k, v in pairs]

    full = encode_repository(pairs)
    return {
        # noise floor: clone + launch + close with nothing modified
        "control": {"repository": None, "usernames": None},
        "reordered": {"repository": encode_repository(list(reversed(pairs)))},
        "missing-file": {"repository": "absent"},
        "empty": {"repository": encode_repository([])},
        "drop-frversion": {
            "repository": encode_repository([p for p in pairs if p[0] != "frversion"])
        },
        "frversion-lower": {"repository": encode_repository(repl("frversion", "1.0"))},
        "frversion-higher": {"repository": encode_repository(repl("frversion", "9.9"))},
        "extra-key": {"repository": encode_repository(pairs + [("fauxcasa", "1")])},
        "corrupt-magic": {"repository": b"\xde\xad\xbe\xef" + full[4:]},
        "truncated": {"repository": full[:-10]},
        # usernames.dat probe: keys are account identifiers (binary analysis,
        # fauxcasa-5kl) — does Picasa preserve a foreign entry?
        "usernames-fake": {
            "repository": None,
            "usernames": encode_repository([("fauxprobe@example.invalid", "1")]),
        },
    }


# ------------------------------------------------------------------- helpers

def sh(*args: str, check: bool = True, **kw) -> subprocess.CompletedProcess:
    return subprocess.run(list(args), check=check, text=True,
                          capture_output=True, **kw)


def census(roots: dict[str, Path]) -> dict[str, tuple[int, str]]:
    """snapshot-relative path -> (size, sha256), mirroring oracle-diff scope."""
    out: dict[str, tuple[int, str]] = {}
    for prefix, root in roots.items():
        if not root.is_dir():
            continue
        for p in sorted(root.rglob("*")):
            if p.is_file():
                data = p.read_bytes()
                out[f"{prefix}/{p.relative_to(root).as_posix()}"] = (
                    len(data), hashlib.sha256(data).hexdigest())
    return out


def clone_roots(prefix: Path, zroot: Path) -> dict[str, Path]:
    google = prefix / APPDATA_GOOGLE
    staged_lib = zroot / LIVE_LIBRARY.relative_to("/")
    return {
        "db3": google / "Picasa2/db3",
        "contacts": google / "Picasa2/contacts",
        "albums": google / "Picasa2Albums",
        "library": staged_lib,
        # not watched by oracle-diff, but a sentinel mismatch could plausibly
        # write registry state (dbVersion lives in Preferences\):
        "registry": prefix,  # filtered to *.reg below by _census_registry
    }


def census_scoped(prefix: Path, zroot: Path) -> dict[str, tuple[int, str]]:
    roots = clone_roots(prefix, zroot)
    reg = roots.pop("registry")
    out = census(roots)
    for p in sorted(reg.glob("*.reg")):
        data = p.read_bytes()
        out[f"registry/{p.name}"] = (len(data), hashlib.sha256(data).hexdigest())
    return out


def diff_census(before: dict, after: dict) -> dict[str, dict]:
    d: dict[str, dict] = {"changed": {}, "new": {}, "removed": {}}
    for k, v in after.items():
        if k not in before:
            d["new"][k] = {"size": v[0], "sha256": v[1]}
        elif before[k] != v:
            d["changed"][k] = {
                "before": {"size": before[k][0], "sha256": before[k][1]},
                "after": {"size": v[0], "sha256": v[1]},
            }
    for k, v in before.items():
        if k not in after:
            d["removed"][k] = {"size": v[0], "sha256": v[1]}
    return d


def wine_cmd(prefix: Path, command: str, *args: str) -> list[str]:
    return [
        "flatpak", "run", "--filesystem=home", f"--command={command}",
        f"--env=WINEPREFIX={prefix}", "org.winehq.Wine", *args,
    ]


def screenshot(dest: Path, tag: str) -> bool:
    """xwd-capture the Picasa window (rootless Xwayland: no root grab)."""
    dest.mkdir(parents=True, exist_ok=True)
    try:
        sh("toolbox", "run", "-c", TOOLBOX, "sh", "-c",
           f'cd "{dest}" && xwd -display {DISPLAY} -name "Picasa 3" -silent '
           f'-out "{tag}.xwd" && magick "{tag}.xwd" "{tag}.png"')
        return True
    except subprocess.CalledProcessError:
        # no window with that name (startup, exit, or a differently-titled
        # dialog) — try every named picasa3.exe window via xwininfo
        try:
            tree = sh("toolbox", "run", "-c", TOOLBOX, "env", f"DISPLAY={DISPLAY}",
                      "xwininfo", "-root", "-tree").stdout
            ids = [ln.split()[0] for ln in tree.splitlines()
                   if "picasa3.exe" in ln and '"' in ln.split("(")[0]]
            ok = False
            for i, wid in enumerate(ids):
                try:
                    sh("toolbox", "run", "-c", TOOLBOX, "sh", "-c",
                       f'cd "{dest}" && xwd -display {DISPLAY} -id {wid} -silent '
                       f'-out "{tag}-{i}.xwd" && magick "{tag}-{i}.xwd" "{tag}-{i}.png"')
                    ok = True
                except subprocess.CalledProcessError:
                    pass
            return ok
        except subprocess.CalledProcessError:
            return False
    finally:
        for f in dest.glob("*.xwd"):
            f.unlink()


# -------------------------------------------------------------------- master

def cmd_master(_: argparse.Namespace) -> None:
    """Clone live prefix + library, retrying until both copies are stable
    (the live oracle session may be writing at any moment)."""
    def live_state() -> dict:
        s = {}
        for root in (LIVE_PREFIX / APPDATA_GOOGLE, LIVE_LIBRARY):
            for p in sorted(root.rglob("*")):
                if p.is_file():
                    st = p.stat()
                    s[str(p)] = (st.st_size, st.st_mtime_ns)
        return s

    for attempt in range(5):
        pre = live_state()
        for dst in (MASTER, MASTER_LIB):
            if dst.exists():
                shutil.rmtree(dst)
        sh("cp", "-a", "--reflink=auto", str(LIVE_PREFIX), str(MASTER))
        sh("cp", "-a", "--reflink=auto", str(LIVE_LIBRARY), str(MASTER_LIB))
        if live_state() == pre:
            db3 = MASTER / APPDATA_GOOGLE / "Picasa2/db3"
            pairs = read_repository(db3 / "repository.dat")
            print(f"master cloned ({len(pairs)} sentinel pairs): {MASTER}")
            return
        print(f"live oracle wrote during copy (attempt {attempt + 1}), retrying...")
        time.sleep(10)
    sys.exit("error: live oracle would not quiesce; try between fixtures")


# ----------------------------------------------------------------- run logic

def setup_clone(name: str, variant: dict) -> tuple[Path, Path]:
    clone = CLONES / name
    zroot = CLONES / f"{name}-z"
    for d in (clone, zroot):
        if d.exists():
            shutil.rmtree(d)
    CLONES.mkdir(parents=True, exist_ok=True)
    sh("cp", "-a", "--reflink=auto", str(MASTER), str(clone))
    staged_lib = zroot / LIVE_LIBRARY.relative_to("/")
    staged_lib.parent.mkdir(parents=True)
    sh("cp", "-a", "--reflink=auto", str(MASTER_LIB), str(staged_lib))

    zlink = clone / "dosdevices/z:"
    zlink.unlink()
    zlink.symlink_to(zroot.resolve())
    users = clone / "drive_c/users/matt"
    for d in USER_DIR_SYMLINKS:
        p = users / d
        if p.is_symlink():
            p.unlink()
            p.mkdir()

    db3 = clone / APPDATA_GOOGLE / "Picasa2/db3"
    for fname in ("repository", "usernames"):
        spec = variant.get(fname)
        if spec is None:
            continue
        target = db3 / f"{fname}.dat"
        if spec == "absent":
            target.unlink()
        else:
            target.write_bytes(spec)
    return clone, zroot


def run_variant(name: str, variant: dict, watch_secs: int) -> dict:
    run_dir = RUNS / name
    if run_dir.exists():
        shutil.rmtree(run_dir)
    run_dir.mkdir(parents=True)
    clone, zroot = setup_clone(name, variant)
    db3 = clone / APPDATA_GOOGLE / "Picasa2/db3"

    for fname in ("repository", "usernames"):
        src = db3 / f"{fname}.dat"
        if src.exists():
            shutil.copy2(src, run_dir / f"{fname}.before.dat")
    before = census_scoped(clone, zroot)

    log = (run_dir / "wine.log").open("wb")
    t0 = time.time()
    # the HOST env must carry DISPLAY too: flatpak exposes the X socket it
    # sees at launch, --env= alone leaves the sandbox displayless (wine
    # then silently runs on the null driver — fauxcasa-5kl runs 1-9)
    env = {k: v for k, v in os.environ.items() if k != "WAYLAND_DISPLAY"}
    env["DISPLAY"] = DISPLAY
    proc = subprocess.Popen(
        ["flatpak", "run", "--filesystem=home", "--socket=x11",
         "--nosocket=wayland", "--nosocket=fallback-x11",
         f"--env=DISPLAY={DISPLAY}", f"--env=WINEPREFIX={clone}",
         "--env=WINEDLLOVERRIDES=mscoree,mshtml=",
         "org.winehq.Wine", PICASA_EXE],
        stdout=log, stderr=log, cwd=REPO, env=env,
    )

    events: list[dict] = []
    seen: dict[str, tuple] = {}
    shot_at = {30, max(60, watch_secs - 15)}
    exited_early: float | None = None
    while time.time() - t0 < watch_secs:
        time.sleep(5)
        t = round(time.time() - t0)
        for fname in ("repository.dat", "usernames.dat"):
            p = db3 / fname
            cur = (p.stat().st_size, p.stat().st_mtime_ns) if p.exists() else None
            if fname in seen and seen[fname] != cur:
                events.append({"t": t, "file": fname,
                               "size": cur[0] if cur else None})
            seen[fname] = cur
        if any(s <= t < s + 5 for s in shot_at):
            screenshot(run_dir / "shots", f"t{t:03d}")
        if proc.poll() is not None:
            exited_early = t
            events.append({"t": t, "event": "picasa exited before close"})
            break

    if exited_early is None:
        sh(*wine_cmd(clone, "wine", "taskkill", "/im", "Picasa3.exe"), check=False)
        try:
            proc.wait(timeout=60)
            events.append({"t": round(time.time() - t0), "event": "clean close"})
        except subprocess.TimeoutExpired:
            events.append({"t": round(time.time() - t0), "event": "close TIMED OUT"})
            screenshot(run_dir / "shots", "stuck-at-close")
    time.sleep(SETTLE_SECS)
    sh(*wine_cmd(clone, "wineserver", "-k"), check=False)
    time.sleep(3)
    log.close()

    after = census_scoped(clone, zroot)
    diff = diff_census(before, after)
    for fname in ("repository", "usernames"):
        src = db3 / f"{fname}.dat"
        if src.exists():
            shutil.copy2(src, run_dir / f"{fname}.after.dat")

    decoded: dict[str, object] = {}
    for fname in ("repository", "usernames"):
        p = run_dir / f"{fname}.after.dat"
        if not p.exists():
            decoded[fname] = "absent"
            continue
        try:
            decoded[fname] = read_repository(p)
        except Exception as e:  # noqa: BLE001 - record, don't crash the matrix
            decoded[fname] = f"unparseable: {e}"

    result = {
        "variant": name,
        "captured": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "watch_secs": watch_secs,
        "exited_early_at": exited_early,
        "events": events,
        "decoded_after": decoded,
        "diff_counts": {k: len(v) for k, v in diff.items()},
        "diff": diff,
    }
    (run_dir / "result.json").write_text(json.dumps(result, indent=1))
    return result


def cmd_run(args: argparse.Namespace) -> None:
    if not MASTER.is_dir():
        sys.exit("error: run `master` first")
    pairs = read_repository(MASTER / APPDATA_GOOGLE / "Picasa2/db3/repository.dat")
    variants = build_variants(pairs)
    names = args.variants.split(",") if args.variants else list(variants)
    unknown = set(names) - set(variants)
    if unknown:
        sys.exit(f"error: unknown variants {sorted(unknown)}; "
                 f"have {sorted(variants)}")
    for i, name in enumerate(names, 1):
        print(f"[{i}/{len(names)}] {name} ...", flush=True)
        r = run_variant(name, variants[name], args.watch)
        print(f"  changed={r['diff_counts']['changed']} "
              f"new={r['diff_counts']['new']} "
              f"removed={r['diff_counts']['removed']} "
              f"events={len(r['events'])} "
              f"early_exit={r['exited_early_at']}", flush=True)
    cmd_report(args)


def cmd_report(_: argparse.Namespace) -> None:
    rows = []
    for rj in sorted(RUNS.glob("*/result.json")):
        r = json.loads(rj.read_text())
        repo_changed = any("repository.dat" in k for k in r["diff"]["changed"])
        lib_writes = [k for b in ("changed", "new", "removed")
                      for k in r["diff"][b] if k.startswith("library/")]
        rows.append((r["variant"], r["diff_counts"], repo_changed,
                     len(lib_writes), r["exited_early_at"],
                     r["decoded_after"]["repository"]))
    print(f"{'variant':18} {'chg/new/rm':>10} {'repo.dat rewritten':>19} "
          f"{'lib writes':>10} {'early exit':>10}")
    for name, dc, rc, lw, ee, dec in rows:
        print(f"{name:18} {dc['changed']:>3}/{dc['new']:>3}/{dc['removed']:>3} "
              f"{str(rc):>19} {lw:>10} {str(ee):>10}")
        if isinstance(dec, list):
            print(f"{'':18}  after: {', '.join(f'{k}={v}' for k, v in dec)}")
        else:
            print(f"{'':18}  after: {dec}")


def cmd_clean(_: argparse.Namespace) -> None:
    for d in (CLONES, MASTER, MASTER_LIB):
        if d.exists():
            shutil.rmtree(d)
            print(f"removed {d}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("master").set_defaults(fn=cmd_master)
    p = sub.add_parser("run")
    p.add_argument("--variants", help="comma-separated subset (default: all)")
    p.add_argument("--watch", type=int, default=WATCH_SECS,
                   help=f"seconds to watch each variant (default {WATCH_SECS})")
    p.set_defaults(fn=cmd_run)
    sub.add_parser("report").set_defaults(fn=cmd_report)
    sub.add_parser("clean").set_defaults(fn=cmd_clean)
    args = ap.parse_args()
    args.fn(args)


if __name__ == "__main__":
    main()
