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
