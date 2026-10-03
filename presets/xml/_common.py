
from __future__ import annotations

import re
import sys
import xml.etree.ElementTree as ET
import xml.parsers.expat as expat
from typing import Optional






class LineElement(ET.Element):
    pass






class _LineTrackingBuilder:


    def __init__(self) -> None:
        self._stack: list[LineElement] = []
        self.root: Optional[LineElement] = None
        self._parser = expat.ParserCreate()
        self._parser.StartElementHandler = self._start
        self._parser.EndElementHandler = self._end
        self._parser.CharacterDataHandler = self._text




        self._parser.EntityDeclHandler = self._entity_decl

    def _entity_decl(self, *args: object) -> None:
        raise expat.ExpatError("entity declarations are not permitted")

    def _start(self, tag: str, attrs: dict[str, str]) -> None:
        elem = LineElement(tag, attrs)
        elem._start_line = self._parser.CurrentLineNumber
        if self._stack:
            self._stack[-1].append(elem)
        else:
            self.root = elem
        self._stack.append(elem)

    def _end(self, tag: str) -> None:
        self._stack.pop()

    def _text(self, data: str) -> None:
        if not self._stack:
            return
        parent = self._stack[-1]
        if len(parent):
            last = parent[-1]
            last.tail = (last.tail or "") + data
        else:
            parent.text = (parent.text or "") + data

    def parse_file(self, path: str) -> LineElement:
        with open(path, "rb") as fh:
            chunk = fh.read(65536)
            while chunk:
                next_chunk = fh.read(65536)
                self._parser.Parse(chunk, not next_chunk)
                chunk = next_chunk
        if self.root is None:
            raise ET.ParseError("empty document")
        return self.root






_CONTAINS_RE = re.compile(
    r"contains\(\s*(@[\w:.-]+)\s*,\s*'([^']*)'\s*\)"
)


def _apply_contains_filter(
    elems: list[ET.Element], xpath: str
) -> list[ET.Element]:









    for m in _CONTAINS_RE.finditer(xpath):
        attr = m.group(1).lstrip("@")
        substring = m.group(2)
        elems = [e for e in elems if substring in (e.get(attr) or "")]
    return elems


def _strip_contains(xpath: str) -> str:



    def _repl_bracket(m: re.Match) -> str:
        inner = m.group(1)

        cleaned = _CONTAINS_RE.sub("", inner).strip()

        cleaned = re.sub(r"^\s*(and|or)\s*", "", cleaned).strip()
        cleaned = re.sub(r"\s*(and|or)\s*$", "", cleaned).strip()
        if cleaned:
            return f"[{cleaned}]"
        return ""

    return re.sub(r"\[([^\]]*contains\([^\]]*)\]", _repl_bracket, xpath)






def load_xml(path: str) -> LineElement:

    try:
        builder = _LineTrackingBuilder()
        return builder.parse_file(path)
    except FileNotFoundError:
        sys.stderr.write(f"ERROR: file not found: {path}\n")
        sys.exit(1)
    except (ET.ParseError, expat.ExpatError) as exc:
        sys.stderr.write(f"ERROR: malformed XML in {path}: {exc}\n")
        sys.exit(1)
    except ValueError as exc:
        sys.stderr.write(f"ERROR: invalid path {path!r}: {exc}\n")
        sys.exit(1)


def split_arg(arg: str, n: int) -> list[str]:



















    if ":::" in arg:
        raw = arg.split(":::")
        if n == 1:
            return [":".join(raw)]
        if n == 2:
            return [raw[0], ":".join(raw[1:])]

        if len(raw) >= 3:
            result = [raw[0], ":".join(raw[1:-1]), raw[-1]]
        elif len(raw) == 2:
            result = [raw[0], raw[1], ""]
        else:
            result = [raw[0], "", ""]
        while len(result) < n:
            result.append("")
        return result[:n]


    if ":" not in arg:
        result: list[str] = [arg]
        while len(result) < n:
            result.append("")
        return result
    if n == 1:
        return [arg]



    _start = (
        2
        if len(arg) > 2 and arg[1] == ":" and arg[0].isalpha() and arg[2] in ("/", "\\")
        else 0
    )
    if n == 2:
        idx = arg.index(":", _start)
        return [arg[:idx], arg[idx + 1:]]

    left = arg.index(":", _start)
    right = arg.rindex(":")
    if left == right:
        result = [arg[:left], arg[left + 1:]]
        while len(result) < n:
            result.append("")
        return result
    field0 = arg[:left]
    fieldn = arg[right + 1:]
    middle = arg[left + 1:right]
    result = [field0, middle, fieldn]
    while len(result) < n:
        result.append("")
    return result[:n]


def element_summary(elem: ET.Element) -> str:

    line = getattr(elem, "_start_line", "?")
    attrs = " ".join(f"@{k}={v}" for k, v in elem.attrib.items())
    parts = [f"{line}:{elem.tag}"]
    if attrs:
        parts.append(attrs)
    if elem.text and elem.text.strip():
        text = elem.text.strip().replace("\n", " ")[:80]
        parts.append(f'text="{text}"')
    return " ".join(parts)


def _split_xpath_steps(xpath: str) -> list[str]:






    steps: list[str] = []
    buf = ""
    depth = 0
    i = 0
    while i < len(xpath):
        c = xpath[i]
        if c == "[":
            depth += 1
            buf += c
        elif c == "]":
            depth -= 1
            buf += c
        elif c == "/" and depth == 0:
            if buf:
                steps.append(buf)
                buf = ""

            if i + 1 < len(xpath) and xpath[i + 1] == "/":
                buf = "//"
                i += 2
                continue
            buf = "/"
        else:
            buf += c
        i += 1
    if buf:
        steps.append(buf)
    return steps


def safe_xpath(root: ET.Element, xpath: str) -> list[ET.Element]:











    if not xpath:
        sys.stderr.write("ERROR: XPath expression is empty\n")
        sys.exit(2)
    try:
        if "contains(" not in xpath:
            return root.findall(xpath)

        steps = _split_xpath_steps(xpath)
        nodes: list[ET.Element] = [root]
        for step in steps:
            if not step or step in (".",):
                continue
            stripped_step = _strip_contains(step)


            query = stripped_step
            if query.startswith("//"):
                query = ".//" + query[2:]
            elif query.startswith("/"):
                query = "./" + query[1:]
            next_nodes: list[ET.Element] = []
            seen: set[int] = set()
            for n in nodes:
                for m in n.findall(query):
                    if id(m) not in seen:
                        seen.add(id(m))
                        next_nodes.append(m)
            if "contains(" in step:
                next_nodes = _apply_contains_filter(next_nodes, step)
            nodes = next_nodes
            if not nodes:
                break

        return [n for n in nodes if n is not root]
    except (SyntaxError, ET.ParseError, TypeError, KeyError) as exc:
        sys.stderr.write(f"ERROR: invalid XPath '{xpath}': {exc}\n")
        sys.exit(2)
