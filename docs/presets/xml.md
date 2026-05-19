# xml

Read-only XPath queries over XML files using stdlib `xml.etree.ElementTree` — no deps beyond Python. Replaces grepping XML by hand or loading a full DOM parser. Three ops cover the common cases: list matches with context, extract a single attribute across all matches, count matches without materializing result text. Useful for i18n files, PHPUnit clover coverage reports, build manifests, and any config-heavy XML codebase.

## Requires

- `python3` (stdlib only — no pip packages)

## Ops

| Op | Syntax | What it returns |
|----|--------|-----------------|
| `xml` | `xml:PATH:XPATH[:full]` | One match per line as `LINE:TAG @attr=val …`. Add `:full` to dump each matched subtree as raw XML. |
| `xml_attr` | `xml_attr:PATH:XPATH:ATTR` | One attribute value per line for each XPath match. Skips elements missing the attribute. |
| `xml_count` | `xml_count:PATH:XPATH` | Single integer — count of XPath matches. Returns `0` (not an error) when nothing matches. |

## Common workflows

**Inspect a config file — find all elements with a specific attribute value:**
```bash
./supertool 'xml:config.xml:.//service[@enabled="true"]'
```
Returns each matching `<service>` element's line number, tag, and attributes — enough to locate and understand it without opening the file.

**Count uncovered lines in a PHPUnit clover report:**
```bash
./supertool 'xml_count:clover.xml:.//line[@count="0"]'
```
Fast path: `xml_count` skips result materialization, so it handles 25 MB / 350K-line reports in ~2.5s.

**Extract all `name` attributes from file nodes matching a class:**
```bash
./supertool 'xml_attr:::clover.xml:::.//file[contains(@name,"CommandX")]:::name'
```
Use `:::` (triple colon) as the field separator when XPath contains colons — avoids ambiguity with the standard `:` separator.

## Configuration

No tokens, no env vars. Inherits global supertool config only.

Memory scales with file size: a 25 MB XML file uses ~150 MB RAM at peak. For very large files, run from an orchestrator and inject the pre-resolved result into the agent context rather than calling inline.

XPath support uses ElementTree's subset: predicates, `contains()`, `//descendant`, `position()`. A built-in shim handles nested `contains()` predicates that ET doesn't natively support. Line numbers come from a custom expat-based parser layered on top.

## Authoring notes

Preset JSON: `presets/xml.json`. Helper scripts: `presets/xml/` — one Python file per op (`query.py`, `attr.py`, `count.py`). The `{path}` placeholder in `cmd` resolves to `presets/xml/` at runtime.
