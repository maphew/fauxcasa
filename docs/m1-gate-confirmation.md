# M1 gate clause 3 — family-archive confirmation

Vehicle for the third clause of the M1 gate (docs/product-spec.md, "M1 —
See your library again"):

> *Gate:* N4 budgets green on the 100k synthetic library; `picasa_db.py
> survey` cross-check shows zero ingest loss on synthetic corpora; **owner
> confirms the same on the family archive.**

Clauses 1 and 2 are automated (perf canaries + `scripts/check-ingest-parity.py`).
Clause 3 is an **owner action**: run the same two checks against the real
family archive and record the result here. This document is the runbook and
the confirmation record. Everything below is designed around the
`privacy-real-picasa-data` rules: the archive is irreplaceable and private —
work on **read-only copies outside the repo**, and only **redacted structural
output** (counts, class names, `<len=N sha1=8hex>` tokens) ever leaves the
machine or lands in this file.

## 1. Prepare read-only copies (owner, once)

Copy — never move, never point tools at the originals — onto local disk,
outside the repo checkout:

| What | Typical Windows source | Copy to (example) |
| --- | --- | --- |
| Photo tree (the watched folders) | wherever the photos live | `X:\archive-copy\library\` |
| Picasa app data | `%LocalAppData%\Google\` (contains `Picasa2\` and `Picasa2Albums\`) | `X:\archive-copy\Google\` |

The app-data copy supplies the db3 catalog (`Picasa2\db3\`), the `.pal`
albums (`Picasa2Albums\`), and `contacts.xml`
(`Picasa2\contacts\contacts.xml`). If any of these never existed in the
archive, the cross-check skips the matching classes and says so.

Optionally mark the copies read-only (`attrib +r /s` or NTFS ACLs) — the
tools below never write into them, but belt-and-braces is in the spirit of
the rules.

## 2. Ingest cross-check (survey vs tracer, redacted)

From the repo checkout:

```
uv run scripts/confirm-archive.py X:\archive-copy\library --picasa-home X:\archive-copy\Google --json X:\archive-copy\confirm.json
```

The script runs both independent readers — `picasa_db.py`'s survey side and
the tracer's `catalog.scan_library` — over the copy and compares per-class
counts, the manifest-free sibling of the synthetic parity gate. Output is
**always redacted**; there is no unredacted mode. Exit 0 with a `PASS` line
is the confirmation signal for the ingest half of the clause. Advisory lines
(db3 caption divergence, unjoinable db3 rows, import-report histogram) are
informational: they describe real-archive residue the spec expects, and do
not fail the check — but read them and note anything surprising in the
record below.

If a strict class FAILs: the output names the class and up to N redacted
example rels. To investigate locally, use `scripts/picasa_db.py` subcommands
directly (they support unredacted output for owner-local use) — but paste
only redacted output anywhere shared, including agent sessions.

## 3. Browse confirmation (frozen bundle, read-only)

Use the frozen artifact, not a source run — the gate is about what a user
would get. Either download the `bundle` workflow artifact from CI for the
commit under test, or build locally with the command at the top of
`apps/desktop-python/fauxcasa-tracer.spec`.

```
dist\fauxcasa-tracer-gui\fauxcasa-tracer-gui.exe X:\archive-copy\library
```

The frozen app writes its own disposable cache under `%XDG_CACHE_HOME%` or
`~\.cache\fauxcasa-tracer` — never into the library. Confirm, by browsing:

- [ ] app starts and the grid populates over the full archive
- [ ] folder tree, albums, starred view, and faces/people all present
- [ ] spot-check a handful of known captions, stars, and album memberships
- [ ] search returns expected hits; slideshow and viewer open stills/RAW/video
- [ ] scrolling the full library feels within the §7 budgets on this machine

## 4. Confirmation record

Append one entry per confirmation run. Paste only the redacted script
output. An entry with both boxes checked and the owner's sign-off line
satisfies gate clause 3 for that commit.

```markdown
### Confirmation — YYYY-MM-DD

- Commit under test: <sha>
- Archive copy prepared: YYYY-MM-DD (read-only copy, outside repo)
- [ ] Ingest cross-check: `confirm-archive.py` exit 0 — PASS
- [ ] Browse confirmation: all checklist items above
- Advisory notes: <anything surprising from the advisory lines, or "none">
- Redacted cross-check output:

    <paste table + PASS line here>

Signed off: <owner> — clause 3 confirmed for this commit.
```

*(no confirmations recorded yet)*
