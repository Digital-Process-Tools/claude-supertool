"""Tests for replace and replace_dry operations."""
import os
import pytest
from supertool import op_replace, dispatch


@pytest.fixture
def tmp_files(tmp_path):
    """Create a temp directory with test files."""
    f1 = tmp_path / "file1.php"
    f1.write_text("LegalStructure::TYPE_CUSTOMER\nother line\nLegalStructure::TYPE_CUSTOMER again\n")

    f2 = tmp_path / "file2.xml"
    f2.write_text('<tag value="{LegalStructure::TYPE_CUSTOMER}" />\n')

    f3 = tmp_path / "no_match.php"
    f3.write_text("nothing interesting here\n")

    sub = tmp_path / "sub"
    sub.mkdir()
    f4 = sub / "deep.php"
    f4.write_text("deep LegalStructure::TYPE_CUSTOMER match\n")

    return tmp_path


class TestReplaceDry:
    """Tests for replace_dry (preview mode)."""

    def test_basic_dry_shows_diff(self, tmp_files):
        result = op_replace("TYPE_CUSTOMER", "TYPE_ENUM_CUSTOMER", str(tmp_files), dry=True)
        assert "DRY RUN" in result
        assert "no files modified" in result
        assert "- " in result  # old line
        assert "+ " in result  # new line

    def test_dry_does_not_modify_files(self, tmp_files):
        f1 = tmp_files / "file1.php"
        original = f1.read_text()
        op_replace("TYPE_CUSTOMER", "TYPE_ENUM_CUSTOMER", str(tmp_files), dry=True)
        assert f1.read_text() == original

    def test_dry_shows_correct_count(self, tmp_files):
        result = op_replace("TYPE_CUSTOMER", "TYPE_ENUM_CUSTOMER", str(tmp_files), dry=True)
        # file1.php has 2 lines with match, file2.xml has 1, sub/deep.php has 1 = 4
        assert "(4 occurrences in 3 files)" in result

    def test_dry_shows_file_paths(self, tmp_files):
        result = op_replace("TYPE_CUSTOMER", "TYPE_ENUM_CUSTOMER", str(tmp_files), dry=True)
        assert "file1.php" in result
        assert "file2.xml" in result
        assert "deep.php" in result
        assert "no_match.php" not in result

    def test_dry_shows_replacement_preview(self, tmp_files):
        result = op_replace("TYPE_CUSTOMER", "TYPE_ENUM_CUSTOMER", str(tmp_files), dry=True)
        assert "TYPE_ENUM_CUSTOMER" in result

    def test_dry_no_matches(self, tmp_files):
        result = op_replace("NONEXISTENT", "REPLACEMENT", str(tmp_files), dry=True)
        assert "0 occurrences" in result

    def test_dry_single_file(self, tmp_files):
        f1 = str(tmp_files / "file1.php")
        result = op_replace("TYPE_CUSTOMER", "TYPE_ENUM_CUSTOMER", f1, dry=True)
        assert "(2 occurrences in 1 files)" in result
        assert "file2.xml" not in result


class TestReplace:
    """Tests for replace (execute mode)."""

    def test_basic_replace(self, tmp_files):
        result = op_replace("TYPE_CUSTOMER", "TYPE_ENUM_CUSTOMER", str(tmp_files), dry=False)
        assert "4 replacements in 3 files" in result
        assert "Done:" in result

    def test_replace_modifies_files(self, tmp_files):
        op_replace("TYPE_CUSTOMER", "TYPE_ENUM_CUSTOMER", str(tmp_files), dry=False)
        f1 = (tmp_files / "file1.php").read_text()
        assert "TYPE_ENUM_CUSTOMER" in f1
        assert "TYPE_CUSTOMER" not in f1

    def test_replace_does_not_touch_non_matching(self, tmp_files):
        original = (tmp_files / "no_match.php").read_text()
        op_replace("TYPE_CUSTOMER", "TYPE_ENUM_CUSTOMER", str(tmp_files), dry=False)
        assert (tmp_files / "no_match.php").read_text() == original

    def test_replace_single_file(self, tmp_files):
        f2 = str(tmp_files / "file2.xml")
        result = op_replace("TYPE_CUSTOMER", "TYPE_ENUM_CUSTOMER", f2, dry=False)
        assert "1 replacements in 1 files" in result
        content = (tmp_files / "file2.xml").read_text()
        assert "TYPE_ENUM_CUSTOMER" in content

    def test_replace_recursive(self, tmp_files):
        op_replace("TYPE_CUSTOMER", "TYPE_ENUM_CUSTOMER", str(tmp_files), dry=False)
        deep = (tmp_files / "sub" / "deep.php").read_text()
        assert "TYPE_ENUM_CUSTOMER" in deep

    def test_replace_receipt_shows_per_file_count(self, tmp_files):
        result = op_replace("TYPE_CUSTOMER", "TYPE_ENUM_CUSTOMER", str(tmp_files), dry=False)
        # file1.php has 2 occurrences
        assert "(2)" in result

    def test_replace_no_matches(self, tmp_files):
        result = op_replace("NONEXISTENT", "REPLACEMENT", str(tmp_files), dry=False)
        assert "0 occurrences" in result


class TestReplaceErrors:
    """Tests for error handling."""

    def test_empty_pattern(self):
        result = op_replace("", "new", ".")
        assert "ERROR" in result
        assert "empty" in result

    def test_same_old_new(self):
        result = op_replace("same", "same", ".")
        assert "ERROR" in result
        assert "identical" in result

    def test_nonexistent_path(self):
        result = op_replace("old", "new", "/nonexistent/path")
        assert "ERROR" in result
        assert "path not found" in result

    def test_empty_path(self):
        result = op_replace("old", "new", "")
        assert "ERROR" in result


class TestReplaceDispatch:
    """Tests for dispatch routing."""

    def test_dispatch_replace_dry(self, tmp_files):
        f1 = str(tmp_files / "file1.php")
        result = dispatch(f"replace_dry:TYPE_CUSTOMER:TYPE_ENUM_CUSTOMER:{f1}")
        assert "--- replace_dry:" in result
        assert "DRY RUN" in result

    def test_dispatch_replace(self, tmp_files):
        f1 = str(tmp_files / "file1.php")
        result = dispatch(f"replace:TYPE_CUSTOMER:TYPE_ENUM_CUSTOMER:{f1}")
        assert "--- replace:" in result
        assert "Done:" in result

    def test_dispatch_replace_no_path_defaults_to_cwd(self, tmp_files):
        result = dispatch(f"replace_dry:XYZNONEXIST99:NEWVAL:{tmp_files}")
        assert "0 occurrences" in result
