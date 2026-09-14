set positional-arguments

default:
    just --list

# Run the current Python desktop prototype.
py *args:
    uv run apps/desktop-python/main.py "$@"

# Run the Python desktop prototype in offscreen smoke mode.
py-smoke *args:
    QT_QPA_PLATFORM=offscreen uv run apps/desktop-python/main.py --quit-after-ready "$@"

# Run Python desktop tests.
py-test *args:
    uv run apps/desktop-python/test_tracer.py "$@"

# Run the Python desktop scroll benchmark.
py-bench *args:
    uv run apps/desktop-python/bench_scroll.py "$@"

# Run the Python desktop vsync probe.
py-vsync *args:
    uv run apps/desktop-python/vsync_probe.py "$@"

# Build the demo library of freely licensed photos (cache/demo-library, downloads once).
demo *args:
    uv run scripts/make-demo-library.py "$@"

# Open the demo library in the app.
py-demo *args:
    uv run apps/desktop-python/main.py cache/demo-library/library --contacts cache/demo-library/contacts/contacts.xml --pal-dir cache/demo-library/albums "$@"

# Capture the documentation screenshot gallery (docs/releases/gallery) from the demo library.
gallery *args:
    uv run scripts/make-gallery.py "$@"
