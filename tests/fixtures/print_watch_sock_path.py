"""Fixture for tests/test_watch_sock_path_581.py.

Imports presets/watch/transport.py fresh and prints its SOCK_PATH constant.
Run as a subprocess (not imported) so the module-level env lookup at
`import transport` time actually re-evaluates against the child's own
environment, rather than reusing whatever the parent test process already
computed on its first import.

Usage: python3 print_watch_sock_path.py <path-to-presets/watch>
"""
import sys

sys.path.insert(0, sys.argv[1])
import transport  # noqa: E402

print(transport.SOCK_PATH)
