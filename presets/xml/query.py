#!/usr/bin/env python3
"""xml:PATH:XPATH[:full] — XPath query over an XML file.

Default: one match per line as 'LINE:TAG @attr=val …'.
:full   : dump matched subtrees as indented XML.

Memory scales with file size (~6× bytes on disk). A 25 MB clover.xml
loads ~150 MB into RAM — acceptable for one-shot CLI use.
"""
from __future__ import annotations

import sys
import xml.etree.ElementTree as ET
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from _common import element_summary, load_xml, safe_xpath, split_arg


def main(arg: str) -> None:
    if not arg:
        sys.stderr.write("ERROR: usage xml:PATH:XPATH[:full]\n")
        sys.exit(2)

    parts = split_arg(arg, 3)
    path, xpath, mode = parts[0], parts[1], parts[2]

    if not path:
        sys.stderr.write("ERROR: usage xml:PATH:XPATH[:full]\n")
        sys.exit(2)

    full_mode = mode.strip().lower() == "full"

    root = load_xml(path)
    matches = safe_xpath(root, xpath)

    if not matches:
        print(f"(0 matches for {xpath!r})")
        return

    if full_mode:
        for elem in matches:
            if hasattr(ET, "indent"):
                ET.indent(elem)
            print(ET.tostring(elem, encoding="unicode"))
    else:
        for elem in matches:
            print(element_summary(elem))


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "")
