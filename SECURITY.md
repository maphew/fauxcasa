# Security Policy

## Supported versions

Fauxcasa is pre-1.0 and under active development. Only the latest 0.1.x
release (including preview/tracer builds) is supported with security fixes.
Older releases are not patched; please upgrade to the latest release before
reporting an issue.

| Version | Supported |
| --- | --- |
| latest 0.1.x | yes |
| older releases / previews | no |

## Reporting a vulnerability

Preferred channel: [GitHub private vulnerability
reporting](https://github.com/maphew/fauxcasa/security/advisories/new) on
this repository. This lets you share reproduction details, logs, or a
proof-of-concept privately with the maintainer before anything is public.

If that link does not work for you (the repository owner must enable
private vulnerability reporting; it may not be turned on yet), please **do
not** post exploit details, crash inputs, or affected file paths in a
public issue. Instead, open a normal [GitHub
issue](https://github.com/maphew/fauxcasa/issues) that only asks for a
private channel to be opened (for example, "I'd like to report a possible
security issue privately — please advise a contact method"), and wait for
a response before sharing specifics.

## Scope

Security reports in scope for this project include:

- Crashes, memory-safety issues, or other exploitable behavior triggered by
  decoding an image, RAW photo, or video file.
- Crashes or exploitable behavior triggered by malformed Picasa metadata
  files (`.picasa.ini`, `Picasa.ini`, `picasa.ini`, `.pal`, `contacts.xml`,
  db3 `.pmp` data, `.picasaoriginals`).
- Any way to escape or weaken the Windows AppContainer sandbox used to
  isolate original-media decoding. The sandbox's security gates are
  exercised by
  `apps/desktop-python/test_decodesvc_win.py`; a report that defeats one of
  those gates is in scope even without a working end-to-end exploit.
- Any code path that writes outside the application's cache directory
  (see `README.md` "Storage And Picasa Data" for where the cache lives).

Out of scope: missing features, UI bugs, performance issues, and anything
without a plausible security impact — please file those as regular issues
instead.

## Privacy when reporting

Do not attach real private photos, real `.picasa.ini` or other Picasa
sidecar files, real db3/`.pmp` data, or any real names, captions, or file
paths from your own library to a report, public or private. Use synthetic
or minimized reproduction files instead. See
`docs/decode-threat-model.md` for the project's threat model and privacy
handling rules for original-media decoding.

## Response expectations

This is a small, largely volunteer-maintained project. Reports are handled
on a best-effort basis; there is no guaranteed response time or SLA. You
will get an acknowledgment and, where possible, a rough sense of severity
and next steps once the report is reviewed.
