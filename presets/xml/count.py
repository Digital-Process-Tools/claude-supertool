#!/usr/bin/env python3
"""xml_count:PATH:XPATH — count XPath matches. Prints one integer.

Returns 0 (not an error) when nothing matches.
Memory scales with file size — does not stream.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from _common import load_xml, safe_xpath, split_arg


def main(arg: str) -> None:
    if not arg:
        sys.stderr.write("ERROR: usage xml_count:PATH:XPATH\n")
        sys.exit(2)

    parts = split_arg(arg, 2)
    path, xpath = parts[0], parts[1]

    if not path or not xpath:
        sys.stderr.write("ERROR: usage xml_count:PATH:XPATH\n")
        sys.exit(2)

    root = load_xml(path)
    matches = safe_xpath(root, xpath)
    print(len(matches))


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "")
