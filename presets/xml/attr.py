#!/usr/bin/env python3
"""xml_attr:PATH:XPATH:ATTR — extract one attribute value per XPath match.

Prints one value per line. Elements missing the attribute are silently skipped.
Use triple-colon escaping (:::) when XPath contains literal colons.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from _common import load_xml, safe_xpath, split_arg


def main(arg: str) -> None:
    if not arg:
        sys.stderr.write("ERROR: usage xml_attr:PATH:XPATH:ATTR\n")
        sys.exit(2)

    parts = split_arg(arg, 3)
    path, xpath, attr = parts[0], parts[1], parts[2]

    if not path or not xpath or not attr:
        sys.stderr.write("ERROR: usage xml_attr:PATH:XPATH:ATTR\n")
        sys.exit(2)

    root = load_xml(path)
    matches = safe_xpath(root, xpath)

    found = 0
    for elem in matches:
        val = elem.get(attr)
        if val is not None:
            print(val)
            found += 1

    if not matches:
        print(f"(0 matches for {xpath!r})")
    elif found == 0:
        print(f"(0 values: matched {len(matches)} elements, none had attribute {attr!r})")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "")
