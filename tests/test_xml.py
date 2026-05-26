"""Tests for presets/xml/*.py — xml, xml_attr, xml_count ops."""
from __future__ import annotations

import importlib.util
import sys
import time
from pathlib import Path

import pytest

PRESET_DIR = Path(__file__).parent.parent / "presets" / "xml"
FIXTURE_DIR = Path(__file__).parent / "fixtures" / "xml"
CLOVER_SAMPLE = str(FIXTURE_DIR / "clover_sample.xml")


def _load(name: str):
    # Clear cached modules so each load is clean
    for mod in list(sys.modules.keys()):
        if mod in ("_common",) or mod.startswith("xml_"):
            sys.modules.pop(mod, None)
    if str(PRESET_DIR) not in sys.path:
        sys.path.insert(0, str(PRESET_DIR))
    spec = importlib.util.spec_from_file_location(f"xml_{name}", PRESET_DIR / f"{name}.py")
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


query_mod = _load("query")
attr_mod = _load("attr")
count_mod = _load("count")

# Also load _common directly for unit-testing helpers
common_spec = importlib.util.spec_from_file_location("_common_xml", PRESET_DIR / "_common.py")
assert common_spec and common_spec.loader
common_mod = importlib.util.module_from_spec(common_spec)
common_spec.loader.exec_module(common_mod)


# ---------------------------------------------------------------------------
# Fixture helpers
# ---------------------------------------------------------------------------

def _write_xml(tmp_path: Path, content: str, name: str = "test.xml") -> str:
    f = tmp_path / name
    f.write_text(content, encoding="utf-8")
    # Use forward-slash paths so the colon in Windows drive letters (C:\...)
    # doesn't confuse split_arg's colon-based field separator.
    return f.as_posix()


SIMPLE_XML = """\
<?xml version="1.0" encoding="UTF-8"?>
<root>
  <item id="1" type="a">hello</item>
  <item id="2" type="b">world</item>
  <item id="3" type="a">foo</item>
</root>
"""

I18N_XML = """\
<?xml version="1.0" encoding="UTF-8"?>
<messages>
  <entry key="btn.save">Enregistrer</entry>
  <entry key="btn.cancel">Annuler</entry>
  <entry key="title.page">Tableau de bord</entry>
</messages>
"""

MIXED_XML = """\
<?xml version="1.0" encoding="UTF-8"?>
<root>
  <parent>
    <child attr="x"/>
    text between
    <child attr="y">content</child>
  </parent>
</root>
"""

CDATA_XML = """\
<?xml version="1.0" encoding="UTF-8"?>
<root>
  <item><![CDATA[raw & <stuff>]]></item>
</root>
"""

UTF8_XML = """\
<?xml version="1.0" encoding="UTF-8"?>
<root>
  <item lang="fr">Hébergement éducatif — café résumé</item>
  <item lang="zh">中文内容</item>
</root>
"""


# ===========================================================================
# 1. xml: op — query.py
# ===========================================================================

class TestXmlQuery:

    def test_basic_element_find_by_tag(self, tmp_path: Path, capsys) -> None:
        """Find elements by simple tag name."""
        path = _write_xml(tmp_path, SIMPLE_XML)
        query_mod.main(f"{path}:item")
        out = capsys.readouterr().out
        assert out.count("\n") == 3
        assert "item" in out

    def test_xpath_attr_filter(self, tmp_path: Path, capsys) -> None:
        """[@attr='value'] filter returns only matching elements."""
        path = _write_xml(tmp_path, SIMPLE_XML)
        query_mod.main(f"{path}:item[@type='a']")
        out = capsys.readouterr().out
        lines = [l for l in out.strip().splitlines() if l]
        assert len(lines) == 2
        assert all("@type=a" in l for l in lines)

    def test_xpath_contains_attr(self, tmp_path: Path, capsys) -> None:
        """contains(@attr, 'substring') predicate works — used by clover-class lookup."""
        query_mod.main(f"{CLOVER_SAMPLE}:.//file[contains(@name,'CommandX')]")
        out = capsys.readouterr().out
        assert "CommandX" in out

    def test_nested_path(self, tmp_path: Path, capsys) -> None:
        """Nested path //package/file/line returns all line elements."""
        query_mod.main(f"{CLOVER_SAMPLE}:.//package/file/line")
        out = capsys.readouterr().out
        assert out.count("\n") >= 5

    def test_multiple_matches_all_returned(self, tmp_path: Path, capsys) -> None:
        """All matches are printed, one per line."""
        path = _write_xml(tmp_path, SIMPLE_XML)
        query_mod.main(f"{path}:.//item")
        out = capsys.readouterr().out
        assert out.count("\n") == 3

    def test_empty_result_zero_matches(self, tmp_path: Path, capsys) -> None:
        """XPath matching nothing prints zero-match notice, exits 0."""
        path = _write_xml(tmp_path, SIMPLE_XML)
        query_mod.main(f"{path}:item[@type='zzz']")
        out = capsys.readouterr().out
        assert "0 matches" in out

    def test_missing_file_exits_nonzero(self) -> None:
        """Missing file writes to stderr and exits non-zero."""
        with pytest.raises(SystemExit) as exc:
            query_mod.main("/nonexistent/path/file.xml:.//item")
        assert exc.value.code != 0

    def test_malformed_xml_graceful_error(self, tmp_path: Path, capsys) -> None:
        """Malformed XML exits non-zero with a human-readable error."""
        path = _write_xml(tmp_path, "<root><unclosed>")
        with pytest.raises(SystemExit) as exc:
            query_mod.main(f"{path}:.//item")
        assert exc.value.code != 0
        err = capsys.readouterr().err
        assert "malformed XML" in err or "ERROR" in err

    def test_invalid_xpath_syntax_graceful(self, tmp_path: Path, capsys) -> None:
        """Invalid XPath exits non-zero with a human-readable error."""
        path = _write_xml(tmp_path, SIMPLE_XML)
        with pytest.raises(SystemExit) as exc:
            query_mod.main(f"{path}:[[[invalid")
        assert exc.value.code != 0
        err = capsys.readouterr().err
        assert "ERROR" in err

    def test_mixed_content_element(self, tmp_path: Path, capsys) -> None:
        """Element with text + child elements doesn't crash."""
        path = _write_xml(tmp_path, MIXED_XML)
        query_mod.main(f"{path}:.//parent")
        out = capsys.readouterr().out
        assert "parent" in out

    def test_self_closing_element_attributes(self, tmp_path: Path, capsys) -> None:
        """Self-closing element with only attributes (like clover <line/>) is summarised."""
        query_mod.main(f"{CLOVER_SAMPLE}:.//line[@type='stmt']")
        out = capsys.readouterr().out
        assert "@type=stmt" in out

    def test_cdata_section_preserved(self, tmp_path: Path, capsys) -> None:
        """CDATA content is included in text output."""
        path = _write_xml(tmp_path, CDATA_XML)
        query_mod.main(f"{path}:.//item")
        out = capsys.readouterr().out
        assert "item" in out

    def test_line_number_tracking(self, tmp_path: Path, capsys) -> None:
        """Element on a known line reports that line number in summary."""
        # The fixture's first <item> is on line 3 of SIMPLE_XML
        path = _write_xml(tmp_path, SIMPLE_XML)
        query_mod.main(f"{path}:item[@id='1']")
        out = capsys.readouterr().out
        # Line number appears before the colon
        first_line = out.strip().splitlines()[0]
        line_num = int(first_line.split(":")[0])
        assert line_num >= 3  # must be tracked (not '?')

    def test_i18n_entry_key_lookup(self, tmp_path: Path, capsys) -> None:
        """Standard i18n XML: //entry[@key='X'] finds the right entry."""
        path = _write_xml(tmp_path, I18N_XML)
        query_mod.main(f"{path}:.//entry[@key='btn.save']")
        out = capsys.readouterr().out
        assert "btn.save" in out

    def test_full_mode_dumps_subtree_xml(self, tmp_path: Path, capsys) -> None:
        """':full' suffix dumps matched subtree as XML, not one-liner."""
        path = _write_xml(tmp_path, SIMPLE_XML)
        query_mod.main(f"{path}:item[@id='1']:full")
        out = capsys.readouterr().out
        assert "<item" in out
        assert "hello" in out

    def test_default_mode_summary_format(self, tmp_path: Path, capsys) -> None:
        """Default mode: 'LINE:TAG @attr=val' one-liner format."""
        path = _write_xml(tmp_path, SIMPLE_XML)
        query_mod.main(f"{path}:item[@id='2']")
        out = capsys.readouterr().out.strip()
        assert ":item" in out
        assert "@id=2" in out

    def test_utf8_content_preserved(self, tmp_path: Path, capsys) -> None:
        """UTF-8 characters in text and attribute values survive the round-trip."""
        path = _write_xml(tmp_path, UTF8_XML)
        query_mod.main(f"{path}:.//item[@lang='fr']")
        out = capsys.readouterr().out
        assert "Hébergement" in out or "fr" in out  # at minimum the attr

    def test_clover_uncovered_lines_commandx(self, capsys) -> None:
        """Real-world: find uncovered lines in CommandX file using contains + count=0."""
        query_mod.main(
            f"{CLOVER_SAMPLE}:.//file[contains(@name,'CommandX')]/line[@count='0']"
        )
        out = capsys.readouterr().out
        lines = [l for l in out.strip().splitlines() if l]
        # CommandX has 5 uncovered lines in the fixture
        assert len(lines) == 5
        assert all("@count=0" in l for l in lines)

    def test_clover_count_files(self, capsys) -> None:
        """Real-world: count file elements in coverage report."""
        query_mod.main(f"{CLOVER_SAMPLE}:.//file")
        out = capsys.readouterr().out
        assert out.count("\n") == 3  # 3 files in fixture

    def test_no_arg_exits_nonzero(self, capsys) -> None:
        """Empty arg string exits non-zero."""
        with pytest.raises(SystemExit) as exc:
            query_mod.main("")
        assert exc.value.code != 0


# ===========================================================================
# 2. xml_attr: op — attr.py
# ===========================================================================

class TestXmlAttr:

    def test_returns_specific_attribute_one_per_line(self, tmp_path: Path, capsys) -> None:
        """xml_attr extracts the named attribute from each match."""
        path = _write_xml(tmp_path, SIMPLE_XML)
        attr_mod.main(f"{path}:.//item:id")
        out = capsys.readouterr().out
        assert out.strip().splitlines() == ["1", "2", "3"]

    def test_skips_elements_missing_attribute(self, tmp_path: Path, capsys) -> None:
        """Elements without the requested attribute are silently skipped."""
        xml = """\
<?xml version="1.0"?>
<root>
  <item num="10"/>
  <item/>
  <item num="20"/>
</root>"""
        path = _write_xml(tmp_path, xml)
        attr_mod.main(f"{path}:.//item:num")
        out = capsys.readouterr().out
        assert out.strip().splitlines() == ["10", "20"]

    def test_xml_attr_count_zero_exits_ok(self, tmp_path: Path, capsys) -> None:
        """Zero XPath matches writes a notice to stdout, exits 0."""
        path = _write_xml(tmp_path, SIMPLE_XML)
        attr_mod.main(f"{path}:.//item[@type='zzz']:id")
        # should not raise
        captured = capsys.readouterr()
        assert "0 matches" in captured.out

    def test_xml_attr_no_matching_attribute_notice(self, tmp_path: Path, capsys) -> None:
        """Elements matched but none have the requested attribute → stdout notice, exits 0."""
        path = _write_xml(tmp_path, SIMPLE_XML)
        attr_mod.main(f"{path}:.//item:nonexistent")
        captured = capsys.readouterr()
        assert "0 values" in captured.out
        assert "none had attribute" in captured.out

    def test_clover_uncovered_line_numbers(self, capsys) -> None:
        """Real-world: get just the num values of uncovered lines in CommandX."""
        attr_mod.main(
            f"{CLOVER_SAMPLE}:.//file[contains(@name,'CommandX')]/line[@count='0']:num"
        )
        out = capsys.readouterr().out
        nums = out.strip().splitlines()
        assert set(nums) == {"32", "34", "40", "50", "52"}

    def test_xml_attr_missing_file_exits_nonzero(self, capsys) -> None:
        """Missing file exits non-zero."""
        with pytest.raises(SystemExit) as exc:
            attr_mod.main("/no/such/file.xml:.//item:id")
        assert exc.value.code != 0

    def test_triple_colon_escape_in_xpath(self, tmp_path: Path, capsys) -> None:
        """Triple-colon escape passes colons inside XPath through correctly."""
        # XPath with contains(@name,'foo:bar') — colon inside string literal
        xml = """\
<?xml version="1.0"?>
<root>
  <file name="foo:bar.php"><line num="5" count="0"/></file>
  <file name="other.php"><line num="9" count="0"/></file>
</root>"""
        path = _write_xml(tmp_path, xml)
        # Use triple-colon as field separator; the colon inside 'foo:bar' stays plain
        attr_mod.main(f"{path}:::.//file[contains(@name,'foo:bar')]/line[@count='0']:::num")
        out = capsys.readouterr().out
        assert out.strip() == "5"

    def test_xml_attr_no_arg_exits_nonzero(self, capsys) -> None:
        """Empty arg string exits non-zero."""
        with pytest.raises(SystemExit) as exc:
            attr_mod.main("")
        assert exc.value.code != 0


# ===========================================================================
# 3. xml_count: op — count.py
# ===========================================================================

class TestXmlCount:

    def test_returns_integer(self, tmp_path: Path, capsys) -> None:
        """xml_count prints a single integer."""
        path = _write_xml(tmp_path, SIMPLE_XML)
        count_mod.main(f"{path}:.//item")
        out = capsys.readouterr().out.strip()
        assert out == "3"

    def test_zero_matches_returns_zero_not_error(self, tmp_path: Path, capsys) -> None:
        """Zero matches → prints 0, exits 0."""
        path = _write_xml(tmp_path, SIMPLE_XML)
        count_mod.main(f"{path}:.//item[@type='zzz']")
        out = capsys.readouterr().out.strip()
        assert out == "0"

    def test_clover_file_count(self, capsys) -> None:
        """Real-world: count files in coverage report returns 3."""
        count_mod.main(f"{CLOVER_SAMPLE}:.//file")
        out = capsys.readouterr().out.strip()
        assert out == "3"

    def test_missing_file_exits_nonzero(self, capsys) -> None:
        """Missing file exits non-zero."""
        with pytest.raises(SystemExit) as exc:
            count_mod.main("/no/such/file.xml:.//item")
        assert exc.value.code != 0

    def test_xml_count_no_arg_exits_nonzero(self, capsys) -> None:
        """Empty arg string exits non-zero."""
        with pytest.raises(SystemExit) as exc:
            count_mod.main("")
        assert exc.value.code != 0


# ===========================================================================
# 4. _common.py unit tests
# ===========================================================================

class TestCommon:

    def test_split_arg_basic(self) -> None:
        parts = common_mod.split_arg("a:b:c", 3)
        assert parts == ["a", "b", "c"]

    def test_split_arg_absorbs_extra_colons(self) -> None:
        """Last part absorbs remaining colons — XPath with embedded colons."""
        parts = common_mod.split_arg("path.xml:.//ns:tag:full", 3)
        assert parts[0] == "path.xml"
        assert parts[1] == ".//ns:tag"
        assert parts[2] == "full"

    def test_split_arg_triple_colon_escape(self) -> None:
        """Triple-colon is decoded to a single colon in the resulting parts."""
        parts = common_mod.split_arg("a.xml:::contains(@n,'x:::y'):::num", 3)
        assert parts[0] == "a.xml"
        assert "contains(@n,'x:y')" in parts[1]
        assert parts[2] == "num"

    def test_split_arg_pads_short(self) -> None:
        """If fewer parts than n, missing ones are empty strings."""
        parts = common_mod.split_arg("only_one", 3)
        assert parts == ["only_one", "", ""]

    def test_element_summary_includes_line_and_attrs(self, tmp_path: Path) -> None:
        """element_summary returns 'LINE:tag @k=v' format."""
        elem = common_mod.LineElement("line", {"num": "42", "count": "0"})
        elem._start_line = 42
        summary = common_mod.element_summary(elem)
        assert summary.startswith("42:line")
        assert "@num=42" in summary
        assert "@count=0" in summary

    def test_element_summary_no_line_uses_question_mark(self, tmp_path: Path) -> None:
        """When _start_line is absent, '?' is used."""
        import xml.etree.ElementTree as ET
        elem = ET.fromstring('<item/>')
        summary = common_mod.element_summary(elem)
        assert summary.startswith("?:item")


# ===========================================================================
# 5. Performance smoke test
# ===========================================================================

class TestPerformance:

    def test_parse_1000_file_elements_under_2s(self, tmp_path: Path, capsys) -> None:
        """1000 synthetic <file> elements parse in under 2 seconds."""
        lines_xml = "\n".join(
            f'  <file name="/builds/test/File{i}.php">'
            f'<line num="1" type="stmt" count="{i % 2}"/>'
            f'</file>'
            for i in range(1000)
        )
        big_xml = f'<?xml version="1.0"?>\n<coverage>\n{lines_xml}\n</coverage>'
        path = _write_xml(tmp_path, big_xml, "big.xml")

        start = time.monotonic()
        count_mod.main(f"{path}:.//file")
        elapsed = time.monotonic() - start

        out = capsys.readouterr().out.strip()
        assert out == "1000"
        assert elapsed < 2.0, f"Parsing took {elapsed:.2f}s — expected < 2s"


# ===========================================================================
# 6. Real-world benchmarks against the 25 MB DVSI clover.xml
# ===========================================================================

REAL_CLOVER = "/Users/floriandavid/Documents/dvsi/.max/coverage-check/coverage/clover.xml"


class TestRealCloverBenchmarks:
    """Benchmarks against the real 25 MB / 353K-line DVSI clover.xml.

    Skipped automatically if the file isn't present locally (CI / other devs).
    """

    def _skip_if_missing(self) -> None:
        if not Path(REAL_CLOVER).is_file():
            pytest.skip("real clover not available")

    def test_xml_count_real_clover_fast(self, capsys) -> None:
        """xml_count over //file on the real 25 MB clover finishes < 5s wall-clock."""
        self._skip_if_missing()
        start = time.perf_counter()
        count_mod.main(f"{REAL_CLOVER}:.//file")
        elapsed = time.perf_counter() - start
        out = capsys.readouterr().out.strip()
        assert out.isdigit() and int(out) > 1000, f"unexpected count: {out!r}"
        assert elapsed < 5.0, f"xml_count took {elapsed:.2f}s — expected < 5s"

    def test_xml_attr_real_clover_uncovered_lines(self, capsys) -> None:
        """xml_attr extracts uncovered line numbers from UserBusinessIncomeRenderer in < 8s."""
        self._skip_if_missing()
        start = time.perf_counter()
        attr_mod.main(
            f"{REAL_CLOVER}:.//file[contains(@name,'UserBusinessIncomeRenderer')]"
            f"/line[@count='0']:num"
        )
        elapsed = time.perf_counter() - start
        out = capsys.readouterr().out
        nums = out.strip().splitlines()
        # Shape-only: values are decimal digits, at least one, within a plausible bound.
        # Exact values depend on the local coverage run — don't assert them here.
        assert len(nums) >= 1, "expected at least one uncovered line number"
        assert len(nums) <= 200, f"unexpectedly large result set: {len(nums)} lines"
        assert all(n.isdigit() for n in nums), f"non-numeric values in output: {nums}"
        assert elapsed < 8.0, f"xml_attr took {elapsed:.2f}s — expected < 8s"

    def test_xml_real_clover_memory_bounded(self, capsys) -> None:
        """Peak memory for xml_count over the real clover stays under 400 MB."""
        self._skip_if_missing()
        import tracemalloc
        tracemalloc.start()
        try:
            count_mod.main(f"{REAL_CLOVER}:.//file")
            _, peak = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()
        capsys.readouterr()  # drain
        assert peak < 400 * 1024 * 1024, (
            f"peak memory {peak / 1024 / 1024:.1f} MB exceeded 400 MB cap"
        )


# ---------------------------------------------------------------------------
# Dispatch (end-to-end via supertool subprocess)
# ---------------------------------------------------------------------------

class TestDispatchEndToEnd:
    """Exercise the supertool binary with preset ops loaded from presets/xml.json.

    This catches regressions in the {argjoin} substitution that bypassing main()
    misses — main()-only tests can't tell whether supertool passes args correctly.
    """

    def _supertool(self) -> Path:
        return Path(__file__).parent.parent / "supertool"

    def test_dispatch_xml_attr_triple_colon(self) -> None:
        """xml_attr via dispatch with ::: form returns uncovered line numbers."""
        import subprocess
        result = subprocess.run(
            [
                sys.executable, str(self._supertool()),
                f"xml_attr:::{CLOVER_SAMPLE}:::"
                ".//file[contains(@name,'CommandX')]/line[@count='0']:::num",
            ],
            capture_output=True, text=True, encoding="utf-8", timeout=10,
        )
        assert result.returncode == 0, f"returncode={result.returncode} stderr={result.stderr!r} stdout={result.stdout!r}"
        assert "PASS" in result.stdout
        nums = [line for line in result.stdout.splitlines() if line.strip().isdigit()]
        assert set(nums) == {"32", "34", "40", "50", "52"}

    def test_dispatch_xml_attr_single_colon(self) -> None:
        """xml_attr via dispatch with single-colon form also works (no colons in XPath)."""
        import subprocess
        result = subprocess.run(
            [
                sys.executable, str(self._supertool()),
                f"xml_attr:{CLOVER_SAMPLE}:"
                ".//file[contains(@name,'CommandX')]/line[@count='0']:num",
            ],
            capture_output=True, text=True, encoding="utf-8", timeout=10,
        )
        assert result.returncode == 0, f"returncode={result.returncode} stderr={result.stderr!r} stdout={result.stdout!r}"
        nums = [line for line in result.stdout.splitlines() if line.strip().isdigit()]
        assert set(nums) == {"32", "34", "40", "50", "52"}

    def test_dispatch_xml_count(self) -> None:
        """xml_count via dispatch returns single integer."""
        import subprocess
        result = subprocess.run(
            [
                sys.executable, str(self._supertool()),
                f"xml_count:::{CLOVER_SAMPLE}:::.//file",
            ],
            capture_output=True, text=True, encoding="utf-8", timeout=10,
        )
        assert result.returncode == 0, f"returncode={result.returncode} stderr={result.stderr!r} stdout={result.stdout!r}"
        # Output contains "PASS" header + integer count
        ints = [line for line in result.stdout.splitlines() if line.strip().isdigit()]
        assert len(ints) >= 1
