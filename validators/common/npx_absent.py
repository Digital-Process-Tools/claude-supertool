































from __future__ import annotations



_GENERIC_MARKERS = (
    "could not determine executable to run",
    "npx canceled due to missing packages",
)


def is_npx_absent(stderr_lower: str, tool: str) -> bool:










    if any(marker in stderr_lower for marker in _GENERIC_MARKERS):
        return True
    return f'unknown command: "{tool}"' in stderr_lower
