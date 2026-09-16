---
title: "json-set has no append verb"
tool: Bash
match: ~(^|[;&|\n])[[:space:]]*(rtk[[:space:]]+(proxy[[:space:]]+)?)?(python3?[[:space:]]+(-m[[:space:]]+)?)?([^[:space:]]*/)?supertool(\.py)?[[:space:]][^|]*'json-set
mode: once,remind
---

**`json-set` only sets a scalar/object leaf at a dotted path — it cannot append to an array.**
Appending needs the array's current length first (a second read-then-write round trip this op's
one-shot design does not offer), so a caller reaching for it to grow a `files` array (the shape
a developer lane's own report JSON updates each follow-up pass) has no op-covered path today.

Two fallbacks, neither ideal: `paste` the whole file (proportional when the file is small, e.g. a
report JSON), or a raw `python3 -c 'json.load / list.append / json.dump'` — which is exactly the
"unvalidated `python3 -c` transform" this op exists to replace, just for the append shape rather
than the field-set shape (#1822). Whether `json-set` should grow an append verb is undecided.
