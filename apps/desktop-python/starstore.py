"""Machine-local star overrides for Tracer's read-only library model.

Space may change a photo's Fauxcasa star without ever writing beside the
original or editing Picasa/EXIF metadata. Overrides therefore live in
Fauxcasa's own machine-local storage and are re-applied after source
metadata indexing or reconciliation.

WHERE (fauxcasa-6vk finding 2): the VARIANT-FREE per-library state dir
(main.library_state_dir), NOT whatever variant cache dir the current walk
happens to use. A star is a user CHOICE; the thumbs/catalog pair beside
it is derived data keyed on the walk (scan filter + excluded extensions),
so keying stars the same way would make a File-Types or --min-image-size
change silently hide every star the user had set. The two directories
coincide for a default walk, which is why the old placement looked right.
"""

from __future__ import annotations

import json
from pathlib import Path

from catalog import Catalog, Photo

STAR_OVERRIDES_NAME = "stars.json"
STAR_OVERRIDES_VERSION = 1
StarKey = tuple[str, str]


def photo_key(photo: Photo) -> StarKey:
    return photo.root_id, photo.rel


def load_star_overrides(state_dir: Path | None) -> dict[StarKey, int]:
    """Load valid 0..5 overrides, failing soft on missing/corrupt files."""
    if state_dir is None:
        return {}
    try:
        data = json.loads((state_dir / STAR_OVERRIDES_NAME).read_text())
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict) or data.get("version") != 1:
        return {}
    rows = data.get("stars")
    if not isinstance(rows, list):
        # A hand-edited or truncated file can carry "stars": null / 3 /
        # {...}; iterating that raises TypeError out of a fail-soft
        # loader and takes the whole launch down (fauxcasa-6vk finding 3).
        return {}
    result: dict[StarKey, int] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        root_id = row.get("root_id")
        rel = row.get("rel")
        star = row.get("star")
        if (isinstance(root_id, str) and isinstance(rel, str)
                and isinstance(star, int) and not isinstance(star, bool)
                and 0 <= star <= 5):
            result[(root_id, rel)] = star
    return result


def apply_star_overrides(catalog: Catalog,
                         overrides: dict[StarKey, int]) -> None:
    """Overlay Fauxcasa-owned choices on freshly imported source stars."""
    for photo in catalog.photos:
        star = overrides.get(photo_key(photo))
        if star is not None:
            photo.star = star


def save_star_overrides(state_dir: Path | None,
                        overrides: dict[StarKey, int]) -> None:
    """Atomically persist overrides; state-dir-less runs remain RAM-only."""
    if state_dir is None:
        return
    state_dir.mkdir(parents=True, exist_ok=True)
    rows = [
        {"root_id": root_id, "rel": rel, "star": star}
        for (root_id, rel), star in sorted(overrides.items())
    ]
    path = state_dir / STAR_OVERRIDES_NAME
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps({
        "version": STAR_OVERRIDES_VERSION,
        "stars": rows,
    }, indent=1))
    tmp.replace(path)
