"""_supertool_edit -- the write-op region, split out of _supertool.py (#2706).

Loaded by `_load_part("_supertool_edit")` from inside `_supertool.py`, at the
exact source position this code used to occupy: a plain `exec(code,
globals())` via `_load_part`, not a real `import`. Every function defined
below therefore has `__globals__ is _supertool.__dict__` once loaded, so
every existing `monkeypatch.setattr(supertool, "op_edit", ...)`-shaped patch
(55 sites measured over this region on #2706's own planning pass) keeps
reaching the code it patches.

Not importable on its own. `_load_part` is the only legitimate loader: it
puts `_load_part` itself into the globals this file executes against before
running it, which is exactly the marker the guard below checks for. A bare
`import _supertool_edit` or `python3 _supertool_edit.py` gets this module's
own fresh globals(), which has no such name, and refuses with a clear
ImportError rather than failing later with a NameError on the first name this
file assumes `_supertool.py` already defined (Dict, Any, os, re, json, ...).

Carries every write-op entry point (`op_replace`, `op_edit`, `op_json_set`,
`op_paste`, `op_append`, `op_replace_lines`), the atomic-write/rollback
machinery they all share (`_atomic_write`, `_result_line`, the retraction and
rollback helpers), the near-miss diagnostic the edit family renders when a
match fails, the newline-census/backslash-warning helpers, and the paste
snapshot/backup machinery -- a contiguous span of the file as it stood on
`eb4b1820` (#2706's lane 0 commit), moved verbatim.
"""
from __future__ import annotations

if "_load_part" not in globals():
    raise ImportError(
        "_supertool_edit.py is a part of _supertool, loaded via "
        "_load_part() (#2706) -- it cannot be imported directly. Run "
        "supertool.py, or `import _supertool` instead."
    )

def op_replace(old: str, new: str, path: str = ".", dry: bool = False) -> str:
    """Find and replace text across files. Supports dry-run preview.

    Searches recursively through `path` (respecting grep file includes),
    finds all occurrences of `old`, and either previews (dry=True) or
    executes (dry=False) the replacement.

    Output format:
      - Dry mode: diff-style preview (- old / + new) per occurrence
      - Execute mode: compact receipt (files modified, counts)
    """
    if not old:
        return "ERROR: empty search pattern\n"
    if old == new:
        return "ERROR: old and new strings are identical\n"
    if not path:
        return "ERROR: empty path\n"

    # Validate path exists
    if path != "." and not os.path.isfile(path) and not os.path.isdir(path):
        return _path_not_found(path, op="replace", creates=True)

    candidates = _grep_candidates(path, _get_exclude_paths("replace"))
    if not candidates:
        return "(0 files to search)\n"

    # Collect matches via whole-file scan so multi-line `old` patterns work.
    # Line-by-line matching would silently miss any pattern containing '\n'.
    # (filepath, [match_start_offsets], effective_old, effective_new). The two
    # effective strings are carried rather than recomputed: this pass and the
    # write pass below used to read the same file with different newline
    # settings, so on a CRLF file one found an LF `old` and the other did not.
    # The write became a no-op and the receipt reported *this* pass's number —
    # `(2 replacements in 2 files)` over two unchanged files (#1049).
    file_matches: List[Tuple[str, List[int], str, str]] = []
    total_count = 0
    for file_path in candidates:
        try:
            with open(file_path, "rb") as f_bin:
                head = f_bin.read(4096)
            if b"\x00" in head:
                # Binary file — skip. Avoids regex-sub corrupting git internals,
                # images, compiled blobs, etc. (recovered cost: a `.git/index`
                # walked into by a stray relative path arg.)
                continue
            with open(file_path, "r", encoding="utf-8",
                      errors="surrogateescape", newline="") as f:
                content = f.read()
        except OSError:
            continue
        positions: List[int] = []
        eff_old, eff_new = old, new
        for nl, cand in _newline_variants(old):
            start = 0
            found: List[int] = []
            while True:
                idx = content.find(cand, start)
                if idx == -1:
                    break
                found.append(idx)
                start = idx + len(cand)
            if found:
                positions = found
                eff_old = cand
                eff_new = _retermed(new, nl) if nl else new
                break
        if positions:
            file_matches.append((file_path, positions, eff_old, eff_new))
            total_count += len(positions)

    if total_count == 0:
        # The one no-op receipt in the tool that does not say ERROR, so it
        # never reached the exit code either (#680). A preview finding nothing
        # is a truthful preview, not a decline — only the real op declines.
        if not dry:
            _bump_counter(_SKIP_COUNT, "cnt_skip")
        return f"(0 occurrences of '{old}' found)\n"

    if dry:
        out: List[str] = [f"({total_count} occurrences in {len(file_matches)} files)\n"]
        # Derived the same way the execute-mode receipt derives it (see the
        # comment above `retermed` in the write branch below): `eff_old !=
        # old` is exactly "a re-terminated variant matched", so this is the
        # same disclosure one branch earlier, over the preview rather than
        # the write (#1069). Per file, not a blanket summary line, for the
        # same reason: a note true of the run and false of one of its files
        # is the failure #1057 already rejected in this op's own execute
        # branch.
        retermed: Dict[str, str] = {}
        for filepath, positions, eff_old, eff_new in file_matches:
            # splitlines, not split("\n"): the effective strings may be CRLF or
            # CR terminated, and a split on "\n" leaves a trailing \r on every
            # preview line.
            old_lines = eff_old.splitlines() or [eff_old]
            new_lines = eff_new.splitlines() or [eff_new]
            if eff_old != old:
                retermed[filepath] = ("CRLF" if "\r\n" in eff_old
                                      else "CR" if "\r" in eff_old else "LF")
            tag = f" [{retermed[filepath]}]" if filepath in retermed else ""
            out.append(f"\n{_fwd(filepath)}{tag}\n")
            try:
                with open(filepath, "r", encoding="utf-8",
                          errors="surrogateescape", newline="") as f:
                    content = f.read()
            except OSError:
                continue
            for pos in positions:
                start_line = _line_number_at(content, pos)
                end_line = start_line + len(old_lines) - 1
                label = f"L{start_line}" if start_line == end_line else f"L{start_line}-L{end_line}"
                out.append(f"  {label}:\n")
                for ol in old_lines:
                    out.append(f"    - {ol}\n")
                for nl in new_lines:
                    out.append(f"    + {nl}\n")
        if retermed:
            # Only where a choice would be made if this ran for real — a
            # uniform file whose `old` matched literally gets no tag and no
            # mention, matching the execute-mode note this mirrors.
            out.append(f"\n  {mark('↳')} line endings: the text you supplied "
                       f"does not match {len(retermed)} of these files byte "
                       f"for byte and would be re-terminated to the "
                       f"convention marked above to make it match — every "
                       f"untouched line would be unchanged\n")
        out.append(f"\nSummary: {total_count} replacements in {len(file_matches)} files (DRY RUN — no files modified)\n")
        return "".join(out)

    # Execute mode
    files_modified: Dict[str, int] = {}
    vanished: List[str] = []
    # Per-file count of occurrences that were already sitting inside a prior
    # application of this same edit (#938, the residual #701 left open:
    # `edit` gained this disclosure, `replace` shared the silent double-apply
    # and had none of it — confirmed live in the issue's own follow-up
    # comment). `_count_already_applied` runs the same positional test
    # `op_edit` uses, batched across every occurrence in one file rather than
    # per-occurrence (see its own docstring for why that batching is not
    # optional), against the PRE-write content — a later occurrence's index
    # would otherwise be read against text this same write already shifted.
    reapplied_counts: Dict[str, int] = {}
    for file_path, _scan_positions, eff_old, eff_new in file_matches:
        try:
            # newline="": see op_edit / op_append. Without it every line of a
            # CRLF file was rewritten to LF by a replace that matched one
            # string (#1049). The scan's positions are deliberately unused
            # here — recounting against the bytes about to be written is the
            # whole point.
            with open(file_path, "r", encoding="utf-8",
                      errors="surrogateescape", newline="") as f:
                content = f.read()
        except OSError:
            continue
        # Counted here, from the bytes about to be written, never from the scan
        # pass. A number taken from the scan survived the write matching
        # nothing and reported replacements that had not happened (#1049).
        hits = content.count(eff_old)
        if not hits:
            vanished.append(file_path)
            continue
        _n_reapplied = _count_already_applied(content, eff_old, eff_new)
        if _n_reapplied:
            reapplied_counts[file_path] = _n_reapplied
        new_content = content.replace(eff_old, eff_new)
        try:
            _atomic_write(file_path, new_content)
            files_modified[file_path] = hits
        except OSError as e:
            return f"ERROR: failed to write {file_path}: {e}\n"

    total = sum(files_modified.values())
    # `_newline_note` states the invariant this discloses: "re-writing the
    # caller's own endings is always disclosed". `op_edit` and
    # `op_replace_lines` call it; `replace` did the same `_newline_variants`
    # re-termination and said nothing, so a CRLF replace rewrote the caller's
    # block in silence (#1049, third pass).
    #
    # `_newline_note` itself is not reusable here and is deliberately not
    # called: it writes one sentence about one file, and `replace` is
    # multi-file with a different answer per file — a summary line true of the
    # run would be false of every file that matched literally. So the fact goes
    # on the file's own line and the sentence is said once.
    #
    # Derived from `eff_old`, not carried out of the scan: `_newline_variants`
    # drops any candidate equal to the literal, so `eff_old != old` is exactly
    # "a re-terminated variant matched", and the resolved scan/write pair above
    # stays untouched.
    retermed: Dict[str, str] = {}
    for _fp, _positions, _eff_old, _eff_new in file_matches:
        if _fp in files_modified and _eff_old != old:
            retermed[_fp] = ("CRLF" if "\r\n" in _eff_old
                             else "CR" if "\r" in _eff_old else "LF")
    # #938: disclose, never refuse — same posture #701 took for `edit`. Bumped
    # once per occurrence found sitting inside a prior application of this
    # same edit, so the shared `[result]` footer's `K re-applied` (see
    # `_result_line`) is honest for `replace` too, not only for `edit`.
    _total_reapplied = sum(reapplied_counts.values())
    if _total_reapplied:
        _bump_counter(_REAPPLY_COUNT, "cnt_reapply", _total_reapplied)
    out = [f"({total} replacements in {len(files_modified)} files)\n"]
    for fp, cnt in sorted(files_modified.items()):
        tag = f" [{retermed[fp]}]" if fp in retermed else ""
        if fp in reapplied_counts:
            tag += f" [{reapplied_counts[fp]} re-applied]"
        out.append(f"  {_fwd(fp)} ({cnt}){tag}\n")
    if reapplied_counts:
        out.append(
            f"\n  {mark('↳')} re-applied: the text this edit produces was "
            f"already present around the anchor in "
            f"{len(reapplied_counts)} file"
            f"{'' if len(reapplied_counts) == 1 else 's'} — this is a SECOND "
            f"application, not a repeat of the first\n"
        )
    if retermed:
        # Only where a choice was made. A uniform file whose `old` matched
        # literally is not marked and produces no line at all — on Windows
        # every file is CRLF, and a note that fires on every call is the noise
        # #1049's second pass removed.
        out.append(f"  {mark('↳')} line endings: the text you supplied did not "
                   f"match {len(retermed)} of these files byte for byte and "
                   f"was re-terminated to the convention marked above to make "
                   f"it match — every untouched line is unchanged\n")
    if vanished:
        # Matched during the scan, gone by the time the write read it back.
        # Dropping these silently is how the count and the disk disagree.
        out.append(f"\n{len(vanished)} file(s) matched during the scan and no "
                   f"longer matched when the write read them back — NOT "
                   f"modified:\n")
        for fp in sorted(vanished):
            out.append(f"  {_fwd(fp)}\n")
    out.append(f"\nDone: '{old}' → '{new}'\n")
    return "".join(out)


def _write_target(path: str) -> str:
    """The path a write to *path* actually lands on — a symlink followed (#1136).

    `_atomic_write` resolves a symlink before `os.replace` so the real file is
    written rather than clobbered by a regular file. Anything that has to reason
    about what that write did to the filesystem — above all the rollback, which
    deletes — has to ask the same question of the same path, or it decides
    identity on an object the writer never touched. One expression, two callers.
    """
    return os.path.realpath(path) if os.path.islink(path) else path


def _write_target_display(path: str) -> str:
    """`_write_target(path)`, spelled the way `path` itself is displayed (#1146).

    `_write_target` uses `os.path.realpath`, which canonicalises every
    symlinked ANCESTOR directory along the way, not only the leaf symlink —
    macOS routes every `/tmp` path through `/private/tmp`. The receipt lines
    that quote `path` back (the success line `_receipt_head` retracts, the
    `[rolled back]`/`[left on disk]` subject) never canonicalise ancestors —
    they use `os.path.abspath`. So a retraction of a write through a symlink
    named the SAME directory two different ways: `/private/var/…/target.py`
    for the resolved target, `/var/…/link.py` for the link the success line
    named, and a reader had to work out the two prefixes were one place.

    Rewritten to agree with `path`'s own spelling wherever the divergence is
    ONLY that ancestor-canonicalisation — i.e. the write target and `path`'s
    directory resolve (via realpath) to the very same directory, and differ
    only in which of the two equivalent spellings was used for it. A target
    that genuinely lives elsewhere (chained symlinks pointing well outside
    `path`'s own directory) is returned exactly as `_write_target` found it —
    this only removes a cosmetic mismatch, never the disclosure that the
    write landed somewhere else.
    """
    target = _write_target(path)
    if target == path:
        return target
    path_dir = os.path.dirname(os.path.abspath(path))
    target_dir = os.path.dirname(target)
    if path_dir != target_dir and os.path.realpath(path_dir) == os.path.realpath(target_dir):
        return os.path.join(path_dir, os.path.basename(target))
    return target


def _process_umask() -> int:
    """The process umask, read once at import.

    CPython offers no getter: the mask has to be set and put back, and the two
    syscalls are a window in which another thread would create a file under the
    wrong mask. Read at import, before this module can have started a thread,
    so the window is never open while one exists.
    """
    cur = os.umask(0o022)
    os.umask(cur)
    return cur


#: What a file `paste`/`append` brings into existence is created at (#1275).
#: `0666 & ~umask` is what `open`, `>`, `tee`, `cp` and every editor produce;
#: the `0600` that reached disk before was `tempfile.mkstemp`'s, an artefact of
#: the temp file rather than a default anybody chose, and it was never a
#: guarantee either -- an overwrite has always kept the target's own mode
#: (#259), so the tight bit applied only to files that were not there before.
#: A caller wanting owner-only files sets `umask 077`, which is what the mask
#: is for, and the receipt states the mode so the widening is not silent.
_NEW_FILE_MODE = 0o666 & ~_process_umask()


def _atomic_write(path: str, content: str) -> None:
    """Write content to path atomically — temp file + os.replace.

    Enforces _safe_path containment (closes #146) — the resolved path must
    live under cwd unless SUPERTOOL_ALLOW_OUTSIDE_CWD=1 is set. Single
    chokepoint for all mutation ops (paste/edit/replace_lines/vim/replace).

    If `path` is a symlink, follow it to the real target — otherwise
    os.replace would clobber the symlink with a regular file, leaving the
    real file untouched (silent data divergence). The symlink target itself
    is checked against cwd containment — a symlink in the repo pointing at
    /etc/hosts is rejected.

    Crash-safe: if interrupted mid-write, the original file is preserved
    (the temp file is incomplete but the target path still has old data).

    Uses surrogateescape encoding so any bytes that round-tripped through
    read(errors='surrogateescape') survive the write unchanged — protects
    files containing illegal UTF-8 sequences (binary blobs, partial encodes).
    """
    import tempfile
    # Containment check happens against the symlink target (real path) so a
    # symlinked write doesn't escape cwd via the symlink itself.
    _safe_path(path)
    # #1147: a pinned target beats re-deriving one, keyed on the very string
    # `_run_with_validators` sampled `_write_target` against -- BEFORE
    # `_expand_home` below can change what `path` spells, so the key matches
    # what the pin was set with rather than a rewritten copy of it.
    _pinned = getattr(_DISPATCH_STATE, "pinned_write_target", None)
    _use_pin = _pinned is not None and _pinned[0] == path
    # Behind that check, and for the same reason as `render_file` (#1300): a
    # direct internal caller must not write to `<cwd>/~/x` after the gate
    # approved the home path.
    path = _expand_home(path)
    # Every mutating op passes through here, which makes it the one place that
    # can promise a coalesced `git status` snapshot never outlives a write
    # (#1126). Cleared before the write rather than after: an exception on the
    # way out would otherwise leave a snapshot describing a tree that has
    # already partly moved.
    _path_meta_bulk_drop()
    # Byte-pattern warnings a syntax validator structurally cannot catch, raised
    # at the one place every mutating op passes through (#380).
    _warn = _sh_backslash_warning(path, content)
    _key = os.path.abspath(path)
    # The sampled value if one is pinned for this exact path (#1147) -- never
    # re-asking `_write_target` a question whose answer may have moved since
    # `_run_with_validators` asked it. Any other caller (a bare internal write,
    # `op_replace`'s own per-file loop) has no pin keyed to its `path` and
    # resolves exactly as before.
    real_path = _pinned[1] if _use_pin else _write_target(path)
    target_dir = os.path.dirname(os.path.abspath(real_path)) or "."
    # Preserve the original file's mode (#259). mkstemp creates the temp file
    # with 0600; os.replace would then clobber the target's mode, silently
    # dropping the executable bit on scripts/git hooks. Capture the existing
    # mode and re-apply it to the temp file before the rename.
    #
    # A brand-new file used to keep mkstemp's 0600 (#1275). That is not a
    # default anybody chose — it is the temp file's own mode leaking through a
    # rename — and it made supertool the only file-creating tool on the box
    # that ignores the umask. It gets `_NEW_FILE_MODE` instead, which never
    # carries an executable bit, so #259's "no spurious +x" still holds.
    # Three states, not two. `FileNotFoundError` is "there is no file here",
    # which is the create. Any other OSError means the mode could not be read
    # from a file that may well exist, and applying a create's default there
    # would rewrite a mode nobody asked about — mkstemp's own is left alone
    # instead, which is what happened before #1275 in every case.
    try:
        orig_mode = stat.S_IMODE(os.stat(real_path).st_mode)
    except FileNotFoundError:
        orig_mode = _NEW_FILE_MODE
    except OSError:
        orig_mode = None
    fd, tmp_path = tempfile.mkstemp(
        prefix=".supertool-", suffix=".tmp", dir=target_dir
    )
    try:
        # Open in binary mode + encode manually so Windows text-mode doesn't
        # translate `\n` → `\r\n` (which corrupts mixed-line-ending content,
        # binary blobs, and any caller that round-tripped through
        # `read(errors='surrogateescape')`).
        with os.fdopen(fd, "wb") as f:
            f.write(content.encode("utf-8", errors="surrogateescape"))
        if orig_mode is not None:
            os.chmod(tmp_path, orig_mode)
        os.replace(tmp_path, real_path)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise
    # Only past the rename: the counter says "bytes reached disk", and the
    # warning queue must describe what is on disk. A write that raised did
    # neither, and counting it would let dispatch treat a failed op as a
    # successful one.
    _bump_counter(_WRITE_COUNT, "cnt_write")
    # A rollback rewrites the same path, so drop any stale entry for it first.
    _drop_write_warnings(path)
    if _warn:
        _WRITE_WARNINGS.append((_key, _warn))
    # A `.py`-only advisory seeded at payload-parse time (#2493), for a
    # doubled-backslash escape sequence the payload's own author already
    # exempted from the write refusal via `literal_backslashes` -- popped
    # (not read) so it cannot outlive the one write it describes.
    _py_advice = _PAYLOAD_PY_ESCAPE_ADVISORY.pop(_key, "")
    if _py_advice:
        _WRITE_WARNINGS.append((_key, _py_advice))


_BRANCH_CACHE: List[Optional[Tuple[str, str]]] = [None]

# Above this, the nearest-line scan is skipped — the diagnostic is a courtesy
# on a failure path and must never become the slow part of a failed edit.
_EDIT_DIAG_MAX_LINES = 20000

_GIT_TIMEOUT_DEFAULT = 5


def _git_timeout() -> int:
    """Budget for the git calls supertool makes about itself (#650).

    Overridable per environment for the same reason SUPERTOOL_LINT_TIMEOUT is
    (#553): a loaded runner occasionally needs room, and that is a fact about
    the runner, never a decision about the product. The shipped default does
    not move — pinned by test_the_suite_budget_does_not_move_the_product_default.
    """
    return _env_int("SUPERTOOL_GIT_TIMEOUT", _GIT_TIMEOUT_DEFAULT, minimum=1)


def _branch_reading() -> Tuple[str, str]:
    """`(branch, why_unavailable)` — three states, not two (#650).

    `("my-feature", "")` git answered and named a branch; `("", "")` git
    answered and there is no branch to name; `("", why)` git did not answer.

    The third state used to be the second. Both were `""`, and `_branch_line()`
    renders `""` as silence, so a receipt whose branch lookup timed out was
    byte-identical to one taken outside a repo — an absence the tool produced,
    read as an absence in the world (docs/validators.md, "Declining instead of
    guessing"). It is the wrong direction to be wrong in: the footer exists to
    catch right-file-wrong-branch, so it fell silent on exactly the run where
    the caller had least idea what state the repo was in.

    A missing git binary stays in the middle state deliberately. Nothing on
    that machine was ever going to name a branch, which is the one honest
    silence — the same line `_vim_render_lint` draws for an uninstalled checker.

    Cached for the process: a single supertool invocation cannot change branch
    mid-call, and a batch of edits would otherwise pay a subprocess each. The
    decline is cached with it — a stalled read is the expensive one to repeat.
    """
    if _BRANCH_CACHE[0] is None:
        branch = ""
        why = ""
        budget = _git_timeout()
        try:
            # symbolic-ref, not rev-parse: it resolves an *unborn* branch (a
            # fresh `git init` before the first commit), where rev-parse fails
            # with "ambiguous argument 'HEAD'". It exits non-zero on a detached
            # HEAD, which is the one case worth a second call.
            r = subprocess.run(
                ["git", "symbolic-ref", "--short", "-q", "HEAD"],
                capture_output=True, text=True, timeout=budget,
                encoding="utf-8", errors="replace",
            )
            if r.returncode == 0:
                branch = r.stdout.strip()
            else:
                d = subprocess.run(
                    ["git", "rev-parse", "--short", "HEAD"],
                    capture_output=True, text=True, timeout=budget,
                    encoding="utf-8", errors="replace",
                )
                if d.returncode == 0 and d.stdout.strip():
                    branch = f"detached HEAD at {d.stdout.strip()}"
        except FileNotFoundError:
            pass
        except subprocess.TimeoutExpired as exc:
            cmd = " ".join(str(a) for a in (exc.cmd or ["git"]))
            why = f"`{cmd}` did not answer within {budget}s"
        except OSError as exc:
            why = f"`git symbolic-ref` could not be run — {exc}"
        _BRANCH_CACHE[0] = (branch, why)
    return _BRANCH_CACHE[0]


def _current_branch() -> str:
    """Current git branch, or "" when there isn't one to report.

    The plain-string contract every caller but the receipt wants; *why* the
    string is empty is `_branch_reading()`'s second value.
    """
    return _branch_reading()[0]


def _branch_line() -> str:
    """`[branch: X]` footer for a mutating op (#381).

    Two near-misses in one session were the same shape — right file, wrong
    branch — and supertool is the thing that knows. A handful of tokens per
    call, against a class of mistake that is otherwise silent until commit time.
    """
    branch, why = _branch_reading()
    if branch:
        return f"[branch: {branch}]\n"
    # A read that failed is not a repo without a branch (#650). Silence is
    # reserved for the second; the first says so and names what stalled.
    if why:
        return f"[branch: UNKNOWN — {why}]\n"
    return ""


def _result_line(ops: int, writes: int, skipped: int = 0,
                 reapplied: int = 0,
                 not_checked: Optional[Sequence[str]] = None,
                 rolled_back: int = 0,
                 validated: Optional[Sequence[Tuple[str, bool, bool]]] = None,
                 left_on_disk: int = 0) -> str:
    """`[result] N ops run, M writes[, K skipped][, K rolled back][, K re-applied]` (#621),
    or, for a read-only `validate:` run, `[result] N files, M with findings,
    K not checked` (#990).

    The receipt a mutating op prints sits ABOVE the `[validators]` block, and a
    long validators block is exactly when a reader reaches for `| tail -4`. So
    the last thing on screen was `git-status : ok` — which describes the
    validators and reads as though it described the edit. A no-match and an
    11-file replace were indistinguishable that way, twice, at real cost: once a
    teammate was told files were unfixed when they had been fixed, once an agent
    reported a no-matched batch edit as landed and sent a broken branch to CI.

    The invariant this pins is stronger than "print the summary last": an op
    which changed nothing must not END with output that looks like an op which
    did. So this is a footer, not a move — the detailed receipt stays where
    positional readers and every existing ordering test expect it, and one
    honest line is repeated below the noise.

    Both numbers come from counters, never from re-reading the prose receipt.
    `ops` is `_MUTATION_ATTEMPTS` (bumped when a mutating op runs, outcome
    unknown); `writes` is `_WRITE_COUNT` (bumped at `_atomic_write`, decremented
    by `_retract_write`), so a rolled-back edit correctly reports 0. In a batch
    that is precisely "requested" vs "applied", which is the single line that
    would have caught the #634 incident: one no-match among five successes.

    `ops == 0` means no mutating op was accounted for and the state cannot be
    determined — this declines rather than guessing a tidy `0 writes`, per the
    three-state contract in docs/validators.md.

    `skipped` names the third state for the ops themselves (#680). `ops` vs
    `writes` already carried the information, but only as a subtraction the
    reader had to perform while suspicious: a batch that reported
    `6 ops run, 4 writes` had dropped two edits, and the branch went to CI.
    A count you must diff is not a signal; a word is. So the word appears
    whenever an op declined, and never otherwise — `0 skipped` on the green
    path is exactly the kind of number a reader learns to stop seeing, which
    is how `4 writes` failed in the first place.

    `re-applied` names a fourth state (#701): the op wrote, and what it wrote
    was already there. Re-running a payload whose anchor survives its own edit
    applies the edit a SECOND time, and pre-fix the two runs differed only by a
    line range — so an identical footer read as "the same thing happened" when
    what happened was another mutation. It is a separate word from `skipped`
    deliberately: a skipped op left the disk alone, a re-applied one did not,
    and collapsing them would degrade `skipped` into "something was odd".

    It is also NOT a failure. `_SKIP_COUNT` drives the exit code so a `&&` chain
    stops on a half-applied batch; a re-apply is a legitimate outcome (appending
    a second repeated element has exactly this shape), so it discloses and exits
    0. Refusing would be guessing at intent, which is the line #680 drew too.

    `not_checked` names a fifth state, and it IS a failure (#665). It is about
    the checkers rather than the op: the edit landed, and a validator the
    operator named in `$SUPERTOOL_REQUIRE_VALIDATORS` produced no verdict about
    the file. Before this, that case reached the reader as
    `1 err  (pre-existing — not from this edit)` in the block above and as
    `1 op run, 1 write` here, and exited 0 — so the one knob that exists to
    stop "the gate is not running" from reading as a pass read as a pass. The
    validators are named on the line rather than counted, because the line has
    to be actionable on its own for `| tail -1` to be the documented read.

    It is also the one state that can reach a reader with `ops == 0`, and so it
    is the one exception to the bail below (#969). `validate:` mutates nothing,
    so it had no footer at all: its non-verdict was visible in the row and in
    the exit code, and absent from the line the docs tell readers to trust. The
    counts stay off that line — there was no op to count.

    `validated` is what makes that footer unconditional for `validate:` (#990).
    #979 emitted it only on a decline, so a clean run still ENDED on the last
    file's own row — a per-file verdict standing where a whole-run one belongs,
    on a multi-file run the same defect as #970's forged row. `0 ops run,
    0 writes` remains the wrong summary of a read-only op (#621), so this branch
    reports what a `validate:` run actually produced instead:

    ```
    [result] 3 files, 1 with findings, 0 not checked
    ```

    File counts, and they do not partition — one file can hold a finding AND a
    checker that declined. `not checked` counts a file where at least one
    validator returned no verdict, `skipped` included: #665 refused to
    *escalate* an optional tool nobody installed, and disclosing it on a count
    line escalates nothing. It is shown as a `0` rather than suppressed, which
    is the opposite of the rule `skipped` follows above and deliberately so — on
    a line whose entire content is counts, the not-checked slice is the one a
    reader must be able to find without knowing whether it fired, and a run that
    could not check something has to say so whether or not anything failed.

    `NOT RUN` stays absent from a clean run. It is the token consumers grep for,
    and rendering `0 validators NOT RUN` would put it in the output of every
    green validate — #621's zero-nobody-reads with a live tripwire attached. The
    #979 clause is appended after the counts when it fires, not instead of them.

    `rolled_back` names a sixth state (#952): the op matched, wrote, failed a
    validator and was reverted. `writes` already excluded it — `_retract_write`
    decrements — but a count is only a signal when the reader is already
    suspicious. In the single-op case the exclusion rendered as a sentence
    (`0 writes — nothing changed on disk`); in a batch where other ops did
    write it rendered as `3 ops run, 2 writes`, an arithmetic mismatch the
    reader has to notice AND explain. It is not `skipped`: a skipped op
    declined and left the disk alone, a rolled-back one wrote and had the write
    undone, and the remedies differ (fix the anchor vs fix the code). It is not
    folded into `nothing changed on disk` either, because a no-match prints
    exactly that too — the two most confusable outcomes on this path.

    `left_on_disk` names a seventh state and it is `rolled_back`'s opposite
    number (#1320): the op CREATED a file, a validator reported a finding on it,
    and no validator that found anything was configured to roll back — so the
    refused artefact is in the tree. Every other outcome of a create was already
    distinguishable here and this one was not: it printed `1 op run, 1 write`,
    the same line a clean create prints, while the block above showed a red row.
    A reader piping to the footer — the documented read — got the write and not
    the refusal, and a reader who saw the refusal had no way to learn the file
    survived it. Counted rather than named, unlike `NOT RUN`: the actionable
    identifier is the path, the path is already on the `[left on disk]` line
    above with the validator that refused it, and a footer that grows by a
    filesystem path per op stops being one line.
    """
    if ops <= 0:
        names = list(dict.fromkeys(not_checked or ()))
        files = list(validated or ())
        if files:
            n = len(files)
            with_findings = sum(1 for _p, f, _nv in files if f)
            unchecked = sum(1 for _p, _f, nv in files if nv)
            line = (f"[result] {n} file{'' if n == 1 else 's'}, "
                    f"{with_findings} with findings, {unchecked} not checked")
            if names:
                line += (f" — {len(names)} validator"
                         f"{'' if len(names) == 1 else 's'} "
                         f"NOT RUN ({', '.join(names)}) — those validators "
                         f"returned no verdict, so the file was NOT checked")
            return line + chr(10)
        if not names:
            return ""
        return (f"[result] {len(names)} validator{'' if len(names) == 1 else 's'} "
                f"NOT RUN ({', '.join(names)}) — those validators returned no "
                f"verdict, so the file was NOT checked" + chr(10))
    line = (f"[result] {ops} {'op' if ops == 1 else 'ops'} run, "
            f"{writes} {'write' if writes == 1 else 'writes'}")
    if skipped > 0:
        line += f", {skipped} skipped"
    if rolled_back > 0:
        line += f", {rolled_back} rolled back"
    if left_on_disk > 0:
        line += f", {left_on_disk} left on disk"
    if reapplied > 0:
        line += f", {reapplied} re-applied"
    files = list(validated or ())
    if files:
        # A `validate:` op inside a batch that also mutated something. Its
        # counts have nowhere else to go — an inner op is at dispatch depth > 1
        # and never renders a footer of its own — so #990's guarantee would
        # have a hole exactly where a reader is least likely to notice it.
        line += (f", validated {len(files)} file"
                 f"{'' if len(files) == 1 else 's'} "
                 f"({sum(1 for _p, f, _nv in files if f)} with findings, "
                 f"{sum(1 for _p, _f, nv in files if nv)} not checked)")
    names = list(dict.fromkeys(not_checked or ()))
    if names:
        line += (f", {len(names)} validator{'' if len(names) == 1 else 's'} "
                 f"NOT RUN ({', '.join(names)})")
    tails = []
    if writes == 0 and left_on_disk == 0:
        # A rolled-back write is not a second application of anything, so this
        # clause wins: the bytes complained about are no longer on disk.
        #
        # Not when a create was left on disk (#1320). In a batch that both
        # reverted one write and kept a refused create, `writes` can net to zero
        # while a refused file is sitting in the tree, and "nothing changed on
        # disk" would then be flatly false about the one path the reader most
        # needs to act on.
        tails.append("nothing changed on disk")
    elif reapplied > 0:
        tails.append("an edit already present in the file was applied again")
    if rolled_back > 0:
        tails.append(
            f"{rolled_back} edit{'' if rolled_back == 1 else 's'} "
            f"{'was' if rolled_back == 1 else 'were'} reverted after "
            f"validation and did NOT land")
    if left_on_disk > 0:
        tails.append(
            f"{left_on_disk} created file{'' if left_on_disk == 1 else 's'} a "
            f"validator refused {'was' if left_on_disk == 1 else 'were'} NOT "
            f"removed and {'is' if left_on_disk == 1 else 'are'} still on disk")
    if names:
        tails.append("those validators returned no verdict, so the file was "
                     "NOT checked")
    if tails:
        line += " — " + "; ".join(tails)
    return line + "\n"


def _normalise_ws(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip()


def _edit_miss_diagnostic(old: str, content: str, new: str = "",
                          path: str = "") -> str:
    """Why `old` didn't match (#380).

    `ERROR: old string not found` was the whole message, so the natural next
    move was a `read` round-trip — the one the payload route exists to save.
    These are the ways a payload comes back close but not exact, ranked by how
    often each was the real cause.
    """
    hints: List[str] = []

    # 0. The replacement is ALREADY in the file — consistent with (but not
    #    proof of) a re-run of a payload that already landed. #701 covers the
    #    case where `new` contains `old` (the anchor survives and the edit
    #    applies a second time); this is the other half, where it does not,
    #    and the second run reports a bare no-match that is character-for-
    #    character what a genuinely wrong anchor prints (#984).
    #
    #    A substring search cannot tell a genuine re-run (the whole
    #    replacement sitting where the edit targeted) from a coincidental
    #    match elsewhere in the file — one relayed report found a line that
    #    merely resembled the replacement, reported it as "this looks like a
    #    re-run", and the reader concluded there was nothing to do when the
    #    anchor was in fact simply missing (#2118). The two explanations have
    #    opposite remedies — one is done, the other needs a new anchor — and
    #    stating one as settled collapses the case where the tool genuinely
    #    cannot tell.
    #
    #    A located fact, not a verdict: the ERROR stands, the exit code stands,
    #    the op is still counted as skipped. Downgrading a failure to a note
    #    because it is probably benign is how a loud bug becomes a quiet one.
    if new and new.strip() and new in content:
        at = content.count("\n", 0, content.index(new)) + 1
        hints.append(
            f"the replacement text is ALREADY present at line {at} — this "
            f"may be a re-run of an edit that already applied, but it could "
            f"also be a coincidental match; if line {at} is not where you "
            f"meant to edit, the anchor is likely just missing"
        )

    # 1. Doubled backslashes, in EITHER direction. TOML literal strings
    #    ('''...''') do not process escapes, so `\\302` in a payload is
    #    backslash-backslash-3-0-2 and can never match a file holding `\302`
    #    -- and the reverse: a file written earlier with two literal
    #    backslashes (e.g. via `literal_backslashes = true`) can never match
    #    a payload's `old` carrying one. #380 named only the first direction;
    #    the second produced a bare nearest-match percentage with no mention
    #    of backslashes at all, which is the "fails quietly" half of #985.
    if "\\\\" in old and old.replace("\\\\", "\\") in content:
        hints.append(
            "the file matches with SINGLE backslashes — TOML literal strings "
            "('''...''') do not process escapes, so `\\\\` in the payload is "
            "two literal backslashes"
        )
    elif "\\" in old and old.replace("\\", "\\\\") in content:
        hints.append(
            "the file matches with DOUBLE backslashes — TOML literal strings "
            "('''...''') do not process escapes, so a single `\\` in the "
            "payload can only match one literal backslash, not two"
        )

    lines = content.splitlines()
    if len(lines) > _EDIT_DIAG_MAX_LINES:
        return "".join(f"  {mark('↳')} {h}\n" for h in hints)

    # 2. Whitespace. Indentation drift is the other half of "close but not
    #    exact", and it is invisible in a diff read by eye.
    if not hints:
        norm_old = _normalise_ws(old)
        if norm_old and "\n" not in old.strip():
            for i, ln in enumerate(lines, 1):
                if _normalise_ws(ln) == norm_old:
                    hints.append(
                        f"line {i} matches ignoring whitespace: {ln.strip()!r} "
                        f"— check indentation"
                    )
                    break

    # 3. Nearest line. Even one line of "closest is line 142: ..." turns a
    #    three-call debug loop into one — when it is right about which line.
    if not hints:
        near = _edit_nearest_hint(old, lines, path)
        if near:
            hints.append(near)

    return "".join(f"  {mark('↳')} {h}\n" for h in hints)


# Below this the hint is withheld: not close enough to be worth a look.
_EDIT_NEAR_FLOOR = 0.6
# Two candidates within this much of each other are a tie, not a ranking. The
# run that filed #1489 got 68% twice, ~800 lines apart, and the difference
# between the two was noise the score could not carry.
_EDIT_NEAR_TIE = 0.02
# Windows char-scored per block anchor. The line-level pass ranks first and is
# O(file); this second pass is O(window²) each, so it is bounded rather than
# left to the corpus.
_EDIT_NEAR_WINDOWS = 20
# `difflib.SequenceMatcher.ratio()` is O(len(a) × len(b)), and
# `_EDIT_DIAG_MAX_LINES` bounds this scan by LINE COUNT — the one dimension a
# minified file is small in. Measured: 60 lines of 40 KB, one anchor of 39 KB,
# 220s for a SINGLE comparison and over 30 minutes for the file. Two bounds,
# because either alone leaves the other open: each comparison is clipped, and
# the total is capped in cells so a file of many long lines cannot spend the
# saving line by line.
_EDIT_NEAR_MAX_CHARS = 1000
_EDIT_NEAR_BUDGET = 20_000_000


def _nearest_clip(text: str) -> str:
    return text[:_EDIT_NEAR_MAX_CHARS]


def _nearest_line_candidates(
        needle: str,
        lines: List[str]) -> Tuple[List[Tuple[float, int]], float, bool]:
    """Every line scoring within `_EDIT_NEAR_TIE` of the best, the best, and
    whether the cost budget ran out before the file did.

    The prefilter bar is `best - tie`, not `best`: a rival that merely EQUALS
    the leader has to be computed, because withholding on a tie is the whole
    point and a skipped line cannot be seen to tie. Identical lines are scored
    once and cached — a file of repeated boilerplate is exactly the input this
    is for, and it is also the one that would otherwise re-score every line.

    An exhausted budget is returned rather than swallowed: what has been
    scored so far is a best-of-a-prefix, and reporting it as the file's
    nearest match is the confidently-wrong hint this whole function is about.
    """
    best = 0.0
    scored: List[Tuple[float, int]] = []
    seen: Dict[str, float] = {}
    needle = _nearest_clip(needle)
    spent = 0
    matcher = difflib.SequenceMatcher(a=needle, autojunk=False)
    for i, ln in enumerate(lines, 1):
        ratio = seen.get(ln)
        if ratio is None:
            bar = best - _EDIT_NEAR_TIE
            clipped = _nearest_clip(ln)
            matcher.set_seq2(clipped)
            if matcher.real_quick_ratio() < bar or matcher.quick_ratio() < bar:
                # The bar only rises, so a line hopeless now stays hopeless.
                seen[ln] = -1.0
                continue
            spent += len(needle) * len(clipped)
            if spent > _EDIT_NEAR_BUDGET:
                return [], best, True
            ratio = matcher.ratio()
            seen[ln] = ratio
        elif ratio < 0:
            continue
        if ratio > best:
            best = ratio
        scored.append((ratio, i))
    return [(r, i) for r, i in scored if r >= best - _EDIT_NEAR_TIE], best, False


def _nearest_block_candidates(
        anchor: List[str],
        lines: List[str]) -> Tuple[List[Tuple[float, int]], float, bool, int]:
    """The same, for a multi-line anchor, scored on the WHOLE block (#1489).

    Scoring the first non-blank line only — what this did until #1489 — reads
    `def handler(request):` and reports whichever copy of that boilerplate
    comes first, at a percentage that is a fact about one line of an anchor
    the caller wrote several lines of. Two passes:

    1. A sliding multiset of whitespace-normalised lines over every window of
       the anchor's height, O(file), which ranks windows by how many of the
       anchor's lines they contain at all. Order-insensitive and exact-match,
       so it locates rather than scores: a real near-miss differs in one line
       and lands at (n-1)/n while boilerplate lands far below it.
    2. The character ratio, on the top-ranked windows only, so the percentage
       printed is comparable with the single-line hint's and the second pass
       is bounded by `_EDIT_NEAR_WINDOWS` regardless of file size.
    """
    n = len(anchor)
    if n > len(lines):
        return [], 0.0, False, 0
    want: Dict[str, int] = {}
    for ln in anchor:
        key = _normalise_ws(ln)
        want[key] = want.get(key, 0) + 1
    norm = [_normalise_ws(ln) for ln in lines]
    have: Dict[str, int] = {}
    matched = 0
    best_hits = 0
    tops: List[int] = []
    top_count = 0
    for i, key in enumerate(norm):
        have[key] = have.get(key, 0) + 1
        if have[key] <= want.get(key, 0):
            matched += 1
        if i >= n:
            gone = norm[i - n]
            if have[gone] <= want.get(gone, 0):
                matched -= 1
            have[gone] -= 1
        if i < n - 1:
            continue
        start = i - n + 2  # 1-based first line of this window
        if matched > best_hits:
            best_hits, tops, top_count = matched, [start], 1
        elif matched == best_hits and matched > 0:
            top_count += 1
            if len(tops) < _EDIT_NEAR_WINDOWS:
                tops.append(start)
    if best_hits <= 0:
        # Not one line of the anchor occurs anywhere. There is no block to
        # point at, and the first line's own score has already been measured
        # to carry no information about the block — so say nothing rather
        # than name a line at a number that looks like evidence.
        return [], 0.0, False, 0
    anchor_text = _nearest_clip("\n".join(anchor))
    matcher = difflib.SequenceMatcher(a=anchor_text, autojunk=False)
    scored: List[Tuple[float, int]] = []
    best = 0.0
    spent = 0
    for start in tops:
        window = _nearest_clip("\n".join(lines[start - 1:start - 1 + n]))
        spent += len(anchor_text) * len(window)
        if spent > _EDIT_NEAR_BUDGET:
            return [], best, True, 0
        matcher.set_seq2(window)
        ratio = matcher.ratio()
        if ratio > best:
            best = ratio
        scored.append((ratio, start))
    near = [(r, i) for r, i in scored if r >= best - _EDIT_NEAR_TIE]
    # More windows tied on the line pass than were char-scored, and every one
    # that WAS scored is still tied: the tie count is a floor, and the hint
    # says "at least N". Returned as its own number rather than smuggled into
    # the candidate list as a negative line — that spelling sorted ahead of
    # every real candidate and was printed as the nearest line (PR review).
    floor = top_count if top_count > len(tops) and len(near) == len(tops) else 0
    return near, best, False, floor


def _nearest_pct(ratio: float, identical: bool) -> str:
    """The percentage, never rounded up into a claim of identity (#1855).

    `f"{r:.0%}"` renders 0.998 as `100%`, and the hint that carries it is
    printed directly under `old string not found`. Two sentences a caller
    cannot both believe: measured on a twelve-line block differing by three
    trailing spaces, `SequenceMatcher.ratio()` is 0.9976209357652657 and the
    receipt said the match was exact.

    Nothing is normalising anything -- the metric is honest and the render was
    not -- so this is fixed where the lie is made rather than in the matcher.
    A near miss reports `>99%`, which is true, is not `100%`, and cannot be
    read as "these strings are the same".
    """
    if not identical and round(ratio * 100) >= 100:
        return ">99%"
    return f"{ratio:.0%}"


def _nearest_diff_kind(anchor: List[str], window: List[str]) -> str:
    r"""Name WHAT KIND of difference the near miss is, or say nothing (#1855).

    The reported cost of the bare percentage was one extra read call spent
    finding a one-line difference the scan had already measured and thrown
    away. The caller's question is never "how close" -- it is "what do I look
    for", and the two answers below send them to different places:

    * `differs only in whitespace` -- indentation drift or a trailing space,
      invisible in a diff read by eye, and the one class where re-reading the
      file tells you nothing at all.
    * `differs in N of M lines` -- an anchor whose CONTENT is wrong, which is
      what an invented or misremembered line looks like.

    Three states, not two, and the third is the one #1855 is actually about.
    Where the compared LINES are equal the percentage is a true 100% -- and a
    bare `100%` under `old string not found` is exactly the self-contradiction
    filed. It is reachable: `splitlines()` drops line endings, and the hint
    strips blank lines off both ends of the anchor, so an anchor can be equal
    line-for-line to a window the byte comparison rejected. Saying nothing
    there would leave the reported defect alive in the one case where the
    number really is 100. So it is NAMED as what it is -- the difference is
    outside the text that was compared -- rather than left to read as identity.
    """
    if anchor == window:
        return ("identical line-for-line, so the difference is outside the "
                "compared text: blank or whitespace-only lines at the ends of "
                "`old`, or line endings")
    if [_normalise_ws(ln) for ln in anchor] == [_normalise_ws(ln) for ln in window]:
        return "differs only in whitespace"
    if len(anchor) == 1:
        # The one case where this says nothing. A single-line hint already
        # prints the file's line verbatim beside the percentage, so
        # "differs in 1 of 1 lines" is the only count arithmetic allows and
        # the reader can see the difference in the text next to it.
        return ""
    same = sum(block.size for block
               in difflib.SequenceMatcher(a=anchor, b=window,
                                          autojunk=False).get_matching_blocks())
    # Anchor lines matched NOWHERE in the window, not lines differing at their
    # own index: a one-line insertion shifts every line after it and a
    # positional count would call that eleven differences. The clamp cannot
    # fire today -- `window` is sliced to `len(anchor)`, so `same ==
    # len(anchor)` implies equality and the branch above already returned --
    # and it is kept because `differs in 0 of 12 lines` under `old string not
    # found` would be this issue again, one rewrite later.
    differing = max(1, len(anchor) - same)
    # #1794 -- naming the count still left a real case costing a second
    # round-trip: two lines that read as identical at a glance (a doubled
    # backslash escaping a quote inside a TOML literal block, invisible to
    # the eye) but differ at one byte. When exactly one line differs AND
    # the two blocks are the same height -- no insertion or deletion
    # shifted anything, so "index i in anchor" and "index i in window"
    # name the same line -- the offset of the first character where that
    # one line diverges is cheap to compute and turns "which of these 12
    # lines" into "which byte of this one line". Left unnamed when heights
    # differ: an insertion could make every positional pair after it
    # differ, and a byte offset picked from the wrong pairing would be
    # confidently wrong, which costs more than saying nothing
    # (docs/validators.md).
    if differing == 1 and len(anchor) == len(window):
        _diff_at = [i for i in range(len(anchor)) if anchor[i] != window[i]]
        if len(_diff_at) == 1:
            _i = _diff_at[0]
            _off = _first_diff_offset(anchor[_i], window[_i])
            return (f"differs in {differing} of {len(anchor)} lines -- "
                    f"first byte diverges at line {_i + 1}, offset {_off}")
    return f"differs in {differing} of {len(anchor)} lines"


def _first_diff_offset(a: str, b: str) -> int:
    """0-based UTF-8 BYTE index of the first byte where *a* and *b* diverge.

    A CHARACTER index would be wrong the moment either line holds a
    multi-byte character before the divergence point -- named as a "byte
    offset" in the hint that calls this (#1794 asked for one explicitly),
    so it has to actually be one rather than a character count that happens
    to coincide with it on ASCII, which is all the tests that shipped
    alongside the first version of this function covered.

    Where one is a prefix of the other, the divergence IS the length of the
    shorter encoded string -- the point past its last byte, which is where a
    caller re-anchoring the line would need to look. `surrogatepass` rather
    than the default strict encoder: a line read back through `read` can
    carry a lone surrogate from an upstream decode-with-replacement, and this
    is a diagnostic computing a position, not a write -- raising here would
    turn "cannot show WHERE" into "cannot show anything at all" for the one
    kind of file that most needs a byte offset instead of a character count.
    """
    ab = a.encode("utf-8", errors="surrogatepass")
    bb = b.encode("utf-8", errors="surrogatepass")
    for _idx, (_ca, _cb) in enumerate(zip(ab, bb)):
        if _ca != _cb:
            return _idx
    return min(len(ab), len(bb))


def _edit_nearest_hint(old: str, lines: List[str], path: str = "") -> str:
    """One line naming where `old` nearly matched — or that it cannot say.

    Three states, not two (docs/validators.md, "Declining instead of
    guessing"): a location, nothing at all, and `cannot suggest` when several
    places score the same. A confidently wrong line number costs more than no
    line number — the caller reads the wrong 30 lines and re-anchors against
    them — so a tie is reported as a tie rather than resolved by file order.
    """
    anchor = old.splitlines()
    while anchor and not anchor[0].strip():
        anchor.pop(0)
    while anchor and not anchor[-1].strip():
        anchor.pop()
    if not anchor:
        return ""
    if len(anchor) == 1:
        cands, best, exhausted = _nearest_line_candidates(anchor[0], lines)
        tie_floor = 0
    else:
        cands, best, exhausted, tie_floor = _nearest_block_candidates(
            anchor, lines)
    if exhausted:
        return ("cannot suggest a nearest match: comparing these lines cost "
                "more than the scan's budget, so what it had found is a "
                "best-so-far and not a best. Anchor on a shorter, more "
                "distinctive snippet")
    if not cands or best < _EDIT_NEAR_FLOOR:
        return ""
    n = len(anchor)
    cands.sort(key=lambda c: (-c[0], c[1]))
    best_i = cands[0][1]
    # Overlapping windows describe one neighbourhood, not two candidates.
    rivals = [i for _r, i in cands[1:] if abs(i - best_i) >= n]
    # `tie_floor` is its own trigger, not a modifier on `rivals` (#1614). It
    # means more windows tied on the line pass than were char-scored at all,
    # and every one that WAS scored is still tied — so the unscored ones are
    # unlocated, and the leader is one sample of a set the tool never saw. For
    # an anchor taller than `_EDIT_NEAR_WINDOWS` all 20 sampled starts sit
    # inside one `n`-line neighbourhood by construction, so `rivals` is empty
    # for arithmetic reasons and gating on it made the floor dead code for
    # exactly the long-anchor case #1489 was filed about.
    if rivals or tie_floor:
        total = tie_floor if tie_floor > 1 + len(rivals) else 1 + len(rivals)
        at_least = "at least " if total > 1 + len(rivals) else ""
        picked = ([best_i] + rivals)[:3]
        shown = ", ".join(str(i) for i in picked)
        # Against what was actually listed, not against a hardcoded 3: with
        # `rivals` empty only the leader is named, and `total - 3` then
        # undercounts the withheld places by two.
        extra = total - len(picked)
        more = f" and {extra} more" if extra > 0 else ""
        # `identical=False` unconditionally: this arm names SEVERAL places, so
        # there is no one window to be identical to, and a bare 100% here would
        # claim every one of them is the anchor.
        return (f"cannot suggest a nearest match: {at_least}{total} places "
                f"score the same ({_nearest_pct(best, False)}) — lines "
                f"{shown}{more}. The anchor does not tell them apart; "
                f"re-anchor on a longer or more distinctive block")
    # The window the percentage was computed against, so the difference can be
    # NAMED rather than left as a number the caller has to go and interpret
    # (#1855). Taken here rather than inside the scorer because the scorer
    # compares a 1000-character clip and this classification wants the whole
    # lines -- and because `identical` is what stops the render claiming 100%.
    window = lines[best_i - 1:best_i - 1 + n]
    kind = _nearest_diff_kind(anchor, window)
    pct = _nearest_pct(best, anchor == window)
    detail = f", {kind}" if kind else ""
    if n == 1:
        return (f"nearest match at line {best_i} ({pct}{_nearest_clip_note(anchor, lines, best_i, n)}{detail}): "
                f"{_elide(lines[best_i - 1], _EDIT_NEAR_MAX_CHARS)!r}")
    end = best_i + n - 1
    where = f": read:{path}:{best_i}-{end}" if path else ""
    return (f"nearest match at lines {best_i}-{end} "
            f"({pct}{_nearest_clip_note(anchor, lines, best_i, n)}{detail}){where}")


def _nearest_clip_note(anchor: List[str], lines: List[str],
                       start: int, n: int) -> str:
    """Say so when the percentage is about a prefix rather than the whole text.

    A number computed over the first 1000 characters of a 40 KB line is not
    wrong, but read as a score for the line it is a claim nobody made."""
    longest = max([len(ln) for ln in anchor]
                  + [len(ln) for ln in lines[start - 1:start - 1 + n]])
    if n > 1:
        longest = max(longest, len("\n".join(anchor)))
    if longest <= _EDIT_NEAR_MAX_CHARS:
        return ""
    return f", scored on the first {_EDIT_NEAR_MAX_CHARS} characters"
# A mutating op prints its full arguments in the section header and then the
# diff underneath, so for a content-heavy edit the old and new strings appear
# twice. Above this many characters the header is rebuilt from the parsed
# fields instead — the diff below is the useful part and already shows what
# changed. Short ops keep their verbatim header (#384).
_HEADER_ARG_MAX = 160
_HEADER_ANCHOR_MAX = 60

_SH_SUFFIXES = (".sh", ".bash", ".zsh", ".ksh")
# A run of backslashes at end of line, plus any whitespace between it and the
# newline. Two ways this is not the continuation it looks like:
#   - PARITY: bash consumes backslashes pairwise from the left, so an even run
#     is all escaped backslashes and the line genuinely ends; an odd run leaves
#     one over to continue it. Only even runs are the bug.
#   - TRAILING WHITESPACE: a backslash followed by spaces or tabs never
#     continues the line, whatever the parity — the escape applies to the
#     space. Invisible in a diff, and it silently ends a command.
_TRAILING_BACKSLASH_RUN = re.compile(r"(\\+)([ \t]*)\r?\n")

# Warnings raised at the write chokepoint, drained by dispatch onto the
# receipt. Keyed by path so a rollback can retract the warning for the
# content it just reverted — the bytes complained about are no longer on
# disk, and a warning about them would be worse than none.
_WRITE_WARNINGS: List[Tuple[str, str]] = []

# One `.py` write-chokepoint advisory per target path, seeded at PAYLOAD
# PARSE time (#2493) and consumed by `_atomic_write` the moment that exact
# path is next written. Not `_WRITE_WARNINGS` itself: that queue is DROPPED
# and re-populated by `_atomic_write` on every write to a path (see
# `_drop_write_warnings` there), which would silently discard an entry
# seeded before dispatch ever reaches the op. A separate dict, popped rather
# than read, means a write that never happens (the op errors before
# `_atomic_write`, or targets a different path than the payload named)
# leaves nothing stuck here for a later, unrelated write to inherit — the
# depth<=1 sweep in `dispatch` clears any leftover the same way
# `_PAYLOAD_WARNINGS` is drained, as a backstop against exactly that.
_PAYLOAD_PY_ESCAPE_ADVISORY: Dict[str, str] = {}

# Bumped when a mutating op RUNS, before its outcome is known — the branch
# footer's signal. A write counter cannot serve that role: `_retract_write`
# decrements on rollback and a failed edit never reaches `_atomic_write` at all,
# so keying the footer on bytes-that-landed silently drops the two cases where a
# wrong-branch hypothesis is most useful. Read once per call by dispatch; never
# decremented.
_MUTATION_ATTEMPTS: List[int] = [0]

# How the running dispatch frame separated its fields — ':::', ':', or '' when
# the fields arrived structured through a payload and nothing was tokenized.
# Read by `_resolve_custom_op`, which is several frames down and sees only the
# already-split `parts`, and exported to the preset subprocess as
# SUPERTOOL_ARG_SEP. A preset that reconstructs the caller's input for a
# refusal has to know which separator to rejoin on: git-commit rejoined on ':'
# whatever the route, so a ':::' inside a message came back as a ':' and the
# suggested repair, pasted, committed bytes the caller never wrote (#946).
_ARG_SEP: List[str] = [":"]

# Exit status of the last custom/preset op run in the current frame, or None
# when none ran. A preset writes no file through `_atomic_write`, so the write
# counter below cannot answer "did it succeed"; this is recorded at the
# subprocess rather than parsed back out of its receipt, for the same reason
# `_result_line`'s counts are.
_CUSTOM_OP_OK: List[Optional[bool]] = [None]

# One entry per preset op that answered with a declared exit value it does not
# declare clear to proceed (#1705), as "op exited N". Read by `_main` as a
# per-call delta, for the same warm-daemon reason as `_SKIP_COUNT` beside it.
# A plain list rather than a dispatch-frame accumulator: custom ops are outside
# `_PARALLEL_SAFE_OPS`, and the reader is the call's exit code rather than any
# one op's footer, so nothing here has to stay attributable to a single op.
_UNCLEAN_VALUE_EXITS: List[str] = []

# Bumped by _atomic_write. Lets dispatch ask 'did this op actually write?'
# instead of sniffing the receipt for an ERROR prefix — receipts are prose
# and not every no-op failure says ERROR (op_replace's zero-match returns
# "(0 occurrences of 'x' found)").
_WRITE_COUNT: List[int] = [0]

# Bumped where a mutating op DECLINES: it ran, it could have written, and it
# deliberately left the disk alone (#680). The third state docs/validators.md
# already defines for validators — `ok`, a finding, `skipped` — applied to the
# ops themselves.
#
# Declared at the decline, never inferred from `attempts - writes`. That
# subtraction looks equivalent and is not: a multi-file `replace` writes more
# times than it was attempted, `replace_dry` writes nothing by design, and a
# validator rollback retracts a write that was genuinely made. Each would be
# mis-reported as a decline, and a skip count that is sometimes wrong is worse
# than none — it is the same absence-read-as-fact this counter exists to stop.
_SKIP_COUNT: List[int] = [0]

# Bumped where a mutating op wrote text the file ALREADY contained at that spot
# (#701) — the fourth state, and the only one that is not a decline. See
# `_edit_already_applied` for why the test is positional rather than
# `new in content`, and `_result_line` for why it is not folded into
# `_SKIP_COUNT`. Never decremented on rollback: `_retract_write` is per-path and
# this counter is not, so a batch rollback could retract the wrong op's
# disclosure. The `writes == 0` clause in `_result_line` covers that case
# honestly instead — "nothing changed on disk" is the stronger statement.
_REAPPLY_COUNT: List[int] = [0]

# Bumped where a write is REVERTED after the fact (#952): the op matched, the
# bytes landed, a validator with `rollback_on_fail` regressed, and the previous
# content was restored. The sixth state, and the one that had no name — see
# `_result_line` for why it is neither `skipped` nor covered by `writes`.
#
# Bumped inside `_retract_write`, which is the single chokepoint both rollback
# paths (formatter and validator) already pass through. Bumping at the two call
# sites instead would make a third rollback path added later silently invisible,
# which is the shape of defect this counter exists to close.
_ROLLBACK_COUNT: List[int] = [0]

# Bumped where a mutating op CREATED a file, a validator reported a finding on
# it, and nothing undid the write (#1320). The seventh state, and the mirror
# image of `_ROLLBACK_COUNT`: same red row above, opposite outcome on disk.
#
# It is not a bug that no rollback fired. `rollback_on_fail` is per-validator
# and four of this repo's own eight set it false on purpose — `ruff`,
# `lsp-diag`, `git-status` and `changelog-fragment` are advisory, and reverting
# an author's bytes over a lint nit is the trade this repo ranks below
# misreporting. What was wrong is that the receipt for that outcome was
# byte-identical to a clean create: `[result] 1 op run, 1 write`, with the
# refused artefact sitting in the tree for `git status`, test collection and
# every later read to find (#1320).
#
# Only a create. An overwrite's leftover bytes are in a file the caller already
# owned and the `edited` line above states that truthfully; a create announced a
# path into existence that a checker then refused, and nothing else on the
# receipt says it is still there.
_LEFT_ON_DISK_COUNT: List[int] = [0]

# Validators that ran and returned no verdict about the file (#665). The state
# `skipped` covers a checker that declined before running; this covers one that
# was asked to run, could not, and had only an `adapter` error to say so with.
#
# Names, not a count, because the reader's next action is installing a tool:
# `2 validators NOT RUN` alone sends them back up to the block this footer
# exists to save them from re-reading.
#
# Appended through `_acc_not_checked()`, which routes to the running op's own
# dispatch frame and reaches this list only when the frame unwinds (#1109). So
# what accumulates here is the whole CALL, which is exactly the scope the exit
# code below wants: the warm daemon reuses the process, so `main` reads a
# per-call delta and truncates back, or one ungated edit would poison the exit
# code of every later call in the same worker (#680). The FOOTER's scope is one
# op, and it is built from the frame instead — a `len()` snapshot here was only
# ever per-op while a single op was appending, which stopped being true the day
# `validate` joined `_PARALLEL_SAFE_OPS`.
_NOT_CHECKED: List[str] = []

# One entry per file a `validate:` op rendered a block for, as
# `(path, had_finding, had_non_verdict)` — the material for the whole-run
# verdict `validate:` had no line for at all on the clean path (#990).
#
# Recorded here rather than re-derived from the rendered rows, for the reason
# `_result_line`'s counts already are: a footer parsed back out of the prose it
# is summarising can only ever agree with the prose, which is exactly the thing
# under suspicion when a reader reaches for it.
#
# Per-call, and reached through the dispatch frame, like `_NOT_CHECKED` above
# and for the same two reasons — the warm daemon reuses the process, and the
# footer's scope is one op rather than one call (#1109).
_VALIDATED_FILES: List[Tuple[str, bool, bool]] = []


def _drop_write_warnings(path: str) -> None:
    """Retract queued warnings for `path` — its write was rolled back."""
    key = os.path.abspath(path)
    _WRITE_WARNINGS[:] = [w for w in _WRITE_WARNINGS if w[0] != key]


def _retract_write(path: str) -> None:
    """A rollback reverted a write: un-count it and drop its warnings.

    Both rollback paths restore the previous bytes with a raw write that never
    reaches `_atomic_write`, so without this the op still looks like it wrote.
    That matters beyond tidiness: the compact header is gated on the counter,
    and a change that did not stick leaves no diff to read it from — the same
    reproducibility gap the header rule exists to avoid.
    """
    _drop_write_warnings(path)
    _bump_counter(_ROLLBACK_COUNT, "cnt_rollback")
    _bump_counter(_WRITE_COUNT, "cnt_write", by=-1)


def _rollback_action(pre_existed: bool, pre_content: Optional[bytes]) -> str:
    """`restore` | `unlink` | `refuse` — what undoing this write actually means.

    Three states, because the two the rollback loop used to have were "we have
    prior bytes" and "we do not", and it read the second as "there is nothing to
    do" (#1088). A created file has no prior bytes and still has an undo: unlink.
    So a `paste` of a new file whose content did not parse printed the red row,
    printed no retraction, and left the artifact — the receipt saying the write
    did not survive validation while the filesystem said it had.

    `unlink` is a delete, so it is gated on provenance rather than on the
    absence of a baseline. `pre_existed` is sampled before the op runs and is
    the only thing that can tell "a path this call brought into being" from "a
    path that was here and got overwritten". Deleting the second would turn a
    rollback into destruction of work the call never wrote, which is the one
    outcome this repository ranks below misreporting.

    The third state is the file that existed and whose bytes could not be read.
    Neither undo is available and there is no safe guess, so the caller is told
    rather than left with a silent skip that reads as "nothing needed doing".
    """
    if not pre_existed:
        return "unlink"
    if pre_content is not None:
        return "restore"
    return "refuse"


def _receipt_head(body: object) -> str:
    """The op's own success line — the first non-blank line of its receipt.

    Shared by the two markers that talk about that line rather than about the
    file: `[rolled back]` retracts it, `[left on disk]` confirms it. One
    implementation, because a filtered read has to find the same string in both
    cases and two copies of "first non-blank line" would drift.
    """
    if isinstance(body, str):
        for ln in body.splitlines():
            if ln.strip():
                return _flat_cell(ln.strip())
    return ""


def _note_left_on_disk() -> None:
    """A refused create stood. Single chokepoint for the counter (#1320).

    Separate from the renderer for the reason `_retract_write` is: a second
    disclosure path added later must not be able to print the marker without
    moving the number the footer reads.
    """
    _bump_counter(_LEFT_ON_DISK_COUNT, "cnt_left_on_disk")


def _left_on_disk_line(names: Sequence[str], path: str, body: object,
                       target: str = "") -> str:
    """`[left on disk]` — the create nothing undid, said in words (#1320).

    The mirror of `_retraction_line`, and deliberately built the same way. It
    quotes the receipt's own `created …` line back so that a filtered read which
    caught the claim catches its qualification adjacent to it — an agent greps
    for `created|ERROR`, and "created, and a checker refused it, and it is still
    there" arrived as three lines that shared no token.

    It CONFIRMS rather than retracts, which is the whole distinction from the
    rollback marker: the file exists, the path is real, and the reader's next
    action is to delete or fix it rather than to re-run the write.

    Why this is not solved by turning `rollback_on_fail` on everywhere: four of
    this repo's eight validators set it false on purpose. `ruff` and `lsp-diag`
    are advisory about style and semantics, `git-status` is not about the file's
    content at all, and `changelog-fragment` refuses shapes an author routinely
    iterates on. Unlinking there would trade a misreport for the destruction of
    a write the caller asked for — the one outcome ranked below misreporting.

    Symlink-aware for the same reason `_retraction_line` is (#1136): the write
    landed on the target, so that is the path still holding the refused bytes.
    """
    head = _receipt_head(body)
    quoted = f' — confirms "{head}"' if head else ""
    subject = _flat_cell(path)
    via = ""
    if target and os.path.abspath(target) != os.path.abspath(path):
        subject = _flat_cell(target)
        via = (f" ({_flat_cell(path)} is a symlink, so the write landed on its "
               f"target and that is the file still holding these bytes)")
    who = ", ".join(_flat_cell(n) for n in names)
    return (f"[left on disk] {who} reported a finding on {subject}, and no "
            f"validator that found something sets rollback_on_fail, so nothing "
            f"undid the write{via}{quoted}; the created file is STILL THERE — "
            f"delete it or fix it before the next read")


def _retraction_line(name: str, verb: str, path: str, body: object,
                     created: bool = False, target: str = "") -> str:
    """Retract a receipt's own success line where a FILTERED read will see it.

    `[rolled back] <tool> regressed; file restored` shares no token with the
    `edited <file> (line N)` printed above it, so `grep -E 'edited|ERROR'` —
    the reported way this output is read — returned the claim and not the undo
    (#952). Quoting the retracted line back means any filter that caught the
    claim catches the retraction, adjacent to it and in the same stream
    position.

    Deliberately NOT by suppressing the claim. An absent line makes "written,
    then reverted" indistinguishable from "never ran", which is the same
    absence-read-as-fact defect wearing different clothes.

    Separator-agnostic: the path is echoed as the op received it, so a Windows
    `pkg\\x.py` renders as the caller typed it. Both interpolated strings go
    through `_flat_cell` because this is a column-0 marker line — the rule
    docs/validators.md states for `[validators]` rows applies verbatim here,
    and a path is attacker-influenceable input.

    Plain double quotes rather than `repr()` around the retracted line: repr
    doubles every backslash, so a Windows receipt would retract
    `'edited pkg\\\\x.py (line 2)'` — text that no longer matches the line it
    is retracting, which defeats the whole point of quoting it.
    """
    head = _receipt_head(body)
    quoted = f' — retracts "{head}"' if head else ""
    # `removed` / `NOT created` on the create path (#1088). "restored" names an
    # earlier state, and a file this call brought into being has none — a reader
    # deciding what to do next needs to know the path is now absent, not that it
    # went back to something.
    undo = "removed" if created else "restored"
    tail = "created" if created else "edited"
    # A write through a symlink lands on the target, so the undo does too
    # (#1136). Saying "link.py removed" was true only while the rollback was
    # deleting the wrong object; now that it deletes the right one, the same
    # sentence would tell a reader their symlink is gone when it is intact.
    subject = _flat_cell(path)
    via = ""
    if target and os.path.abspath(target) != os.path.abspath(path):
        subject = _flat_cell(target)
        via = (f" ({_flat_cell(path)} is a symlink, so the write landed on its "
               f"target and that is what was undone; the link is intact)")
    return (f"[rolled back] {name} {verb}; {subject} {undo}{via}{quoted}"
            f"; the file was NOT {tail}")


def _elide(s: str, limit: int) -> str:
    """One-line, length-capped rendering of an op argument for a header.

    Reports the elided character count rather than trailing off — a silent
    truncation reads as "that was the whole argument".
    """
    s = s.replace("\r\n", "⏎").replace("\n", "⏎")
    if len(s) <= limit:
        return s
    return f"{s[:limit]}… (+{len(s) - limit} chars)"


def _commit_header_arg(parts: List[str]) -> str:
    """`git-commit`'s header, for a commit that LANDED (#946, #1235).

    Here the argument is also the artifact: after a successful commit
    `git log -1` hands the message back, so replaying it above a receipt
    whose whole job is to prove the commit landed is a second copy of
    something already retrievable. Commit messages in this repo are long by
    convention, so that echo routinely outweighs the receipt it introduces.

    Success only, and that is the point rather than a detail. On a refusal
    nothing was committed and this header is the ONLY surviving copy of a
    message the caller composed — eliding it there would cause precisely the
    loss #1235 was filed about. The swap in `_dispatch_impl` is gated on the
    op having succeeded for that reason.

    Subject plus a body line COUNT, never a body sample. A sample reads as
    the message and is not; the count is what tells the reader the tool
    received the lines they sent, which is the whole job of an echo.
    """
    msg = parts[1] if len(parts) > 1 else ""
    paths = [p for p in parts[2:] if p]
    # CRLF normalised first: `split` on the newline alone leaves a trailing
    # CR on every line, and `_elide` only collapses the pair — so a message
    # composed on Windows would put a stray CR inside the quoted subject.
    lines = msg.replace(chr(13) + chr(10), chr(10)).split(chr(10))
    out = 'git-commit: "' + _elide(lines[0], _HEADER_ANCHOR_MAX) + '"'
    if len(lines) > 1:
        out += f" +{len(lines) - 1} more message lines"
    if paths:
        out += (f" → {len(paths)} path(s): "
                + _elide(", ".join(paths), _HEADER_ANCHOR_MAX))
    else:
        out += " → no paths (commits the index)"
    return out


def _compact_header_arg(op: str, parts: List[str], sep: str = ":") -> str:
    """Identifying header for a content-heavy mutating op, or "" to keep the
    verbatim one. Each op keeps whatever identifies the *target* — path, line
    range, anchor — and drops the content the diff is about to show anyway.

    *sep* is how this call's fields were split. It gates `git-commit` only:
    under single-colon tokenization `parts[1]` is a fragment of the message
    rather than the message, so a summary built from it would state a subject
    the caller never wrote.
    """
    def _p(i: int) -> str:
        return parts[i] if len(parts) > i else ""

    if op == "git-commit":
        return _commit_header_arg(parts) if sep == ":::" else ""
    if op in ("edit", "replace", "replace_dry"):
        return f'{op}: "{_elide(_p(1), _HEADER_ANCHOR_MAX)}" → {_p(3)}'
    if op == "replace_lines":
        return f"{op}: {_p(1)} lines {_p(2)}-{_p(3)}"
    if op in ("paste", "append"):
        content = ":".join(parts[2:])
        return f"{op}: {_p(1)} ({len(content)} chars)"
    if op == "vim":
        return f"{op}: {_p(1)} {_elide(':'.join(parts[2:]), _HEADER_ANCHOR_MAX)}"
    return ""


def _payload_header_arg(op: str, target: str) -> str:
    """Header for a sub-op that arrived through an @payload, not the colon CLI.

    A batch sub-op used to be echoed back as its fields joined on ':'. The
    payload route exists *because* the content contains ':', so that join does
    not merely lose information — it produces a string that parses as a
    DIFFERENT op. `replace` on `time: 10:30` rendered as

        --- replace:time: 10:30:time: 11:45:/tmp/h.txt ---

    which, pasted back, sends the dispatcher looking for a file named `30`.
    A header is the thing a reader trusts to reconstruct what happened, and in
    a bug report it is often the only surviving record. Inviting them to run an
    op that touches a path nobody named is worse than telling them nothing.

    So this does not attempt a faithful one-line colon rendering — for a
    payload op there isn't one. It names the ROUTE and the TARGET, which is
    what identifies the step, and the `@payload` reference does not resolve to
    a file, so pasting it fails loudly instead of quietly doing something else.
    That is the invariant: a header must never be a runnable string that runs
    something other than what ran; if it cannot be re-runnable it must not look
    re-runnable. Same family as #621 — output presenting itself as a faithful
    account of an operation and not being one. #644.
    """
    return f"{op}:@payload" + (f" → {target}" if target else "")


# Positional colon-argument order for batch sub-ops that have no @payload
# route of their own. Ordering here mirrors the `syntax` strings in
# .supertool.json.
#
# A single-argument op does not need an entry to be ORDERED — but it needs one
# to be CHECKED. Without it the fallback below places whichever lone key the
# payload happened to carry, under no name at all, so a wrong field name is
# indistinguishable from the right one (#1407). Declare the single field
# wherever getting the name wrong produces a plausible-looking run rather than
# an obvious failure.
_BATCH_POSITIONAL_FIELDS: Dict[str, Tuple[str, ...]] = {
    "head":        ("path", "n"),
    "tail":        ("path", "n"),
    "tree":        ("path", "depth"),
    "around_line": ("path", "line", "n"),
    "diff":        ("path1", "path2"),
    # `batch` takes one argument, so the single-field fallback below would have
    # placed it — but that fallback places whatever key happens to be there,
    # under no name. `file = "inner.toml"` therefore ran as `batch:inner.toml`
    # and was refused for the missing `@`, never for the wrong field (#1407).
    # Declaring the single field turns the key itself into something that can
    # be checked.
    "batch":       ("path",),
}


def _ordered_batch_fields(op: str, item: Dict[str, Any]) -> Tuple[List[str], str]:
    """Positional colon fields for a batch sub-op with no @payload route.

    Returns (fields, error) — exactly one is non-empty.

    This replaces `sorted(item)`, which ordered the payload's fields
    ALPHABETICALLY and is not any op's argument order. That was not a header
    defect: it is the arg that gets dispatched. `{"op": "tree", "path": "src/",
    "depth": 2}` ran as `tree:2:src/`, and `between` with `symbol` + `path` ran
    as `between:<path>:<symbol>` — silently searching for the file inside the
    symbol name.

    Where the order is declared it is used; where it is not, this declines
    rather than inventing one. Guessing is how the original defect happened.
    #644.
    """
    lower = {str(k).lower(): v for k, v in item.items() if str(k).lower() != "op"}
    if not lower:
        return [], ""
    order = _BATCH_POSITIONAL_FIELDS.get(op)
    if order is None:
        if len(lower) == 1:
            return [str(next(iter(lower.values())))], ""
        return [], (
            f"ERROR: batch sub-op '{op}' takes its arguments positionally and has "
            f"no declared payload field order, so {_flat_keys(sorted(lower))} "
            f"cannot be placed. Ordering them alphabetically is a guess, and a "
            f"wrong guess dispatches a different op — so this declines instead. "
            f"Use the colon form for '{op}', or an op with an @payload route.\n"
        )
    unknown = sorted(k for k in lower if k not in order)
    if unknown:
        return [], (
            f"ERROR: unknown field(s) {_flat_keys(unknown)} in batch '{op}' "
            f"— accepted: {', '.join(order)}\n"
        )
    fields: List[str] = []
    for name in order:
        if name not in lower:
            break
        fields.append(str(lower[name]))
    skipped = [n for n in order[len(fields):] if n in lower]
    if skipped:
        return [], (
            f"ERROR: batch '{op}' payload sets {', '.join(skipped)} without the "
            f"earlier positional field(s) {', '.join(order[len(fields):order.index(skipped[0])] or order[:1])} "
            f"— colon arguments cannot be sparse.\n"
        )
    return fields, ""


def _sh_backslash_warning(path: str, content: str) -> str:
    """Flag `\\\\` at end-of-line in a shell script (#380).

    The trap is that getting the escaping wrong does not always fail to match —
    sometimes it writes. `FOO=$(cmd \\\\` + newline is syntactically valid bash
    (an escaped backslash, then a new command), passes `bash -n`, passes the
    bash-check validator, and does something entirely different from what was
    meant. The validator cannot see it; only the byte pattern can.
    """
    if not path.endswith(_SH_SUFFIXES):
        return ""
    trailing_ws = False
    even_run = False
    for m in _TRAILING_BACKSLASH_RUN.finditer(content):
        if m.group(2):
            trailing_ws = True
        elif len(m.group(1)) % 2 == 0:
            even_run = True
    if not (trailing_ws or even_run):
        return ""
    if trailing_ws and not even_run:
        return (
            f"{mark('⚠')} {path}: a line ends with a backslash followed by "
            f"whitespace. That is not a line continuation — the backslash "
            f"escapes the space, the command ends there, and the difference "
            f"is invisible in a diff.\n"
        )
    return (
        f"{mark('⚠')} {path}: a line ends with `\\\\`. In bash that is an "
        f"escaped backslash, not a line continuation — it parses cleanly and "
        f"runs differently. This is a note and not a refusal: these bytes "
        f"have no second spelling here, so blocking them would strand the "
        f"caller who meant them. A literal payload block refuses the same "
        f"pattern, because there a second spelling exists (#835).\n"
    )


def _edit_already_applied(content: str, old: str, new: str, idx: int) -> bool:
    """Is the `old` at `idx` sitting INSIDE text this same edit already made?

    The re-run case (#701): `new` contains `old`, so the anchor survives its own
    edit and matches again on a second run. `content[idx:idx+len(old)]` is then
    literally a substring of an occurrence of `new` that a previous run wrote.

    Positional containment, not `new in content`. The issue floated the simpler
    test and it has a real failure mode: `new` may legitimately pre-exist
    somewhere unrelated (an edit inserting `return None` into a file that
    already has a `return None` elsewhere), and a signal that fires on a first
    application is noise — which is how a footer count stops being read. Asking
    instead whether the anchor being replaced is bracketed by an existing copy
    of `new` is the literal statement "this edit's result is already here".

    Deliberately says nothing about intent. An append of a second repeated
    element is indistinguishable from an accidental re-run and must stay
    allowed; the caller decides, the tool discloses.
    """
    if len(new) <= len(old) or old not in new:
        return False
    end = idx + len(old)
    j = content.find(new)
    while j != -1 and j <= idx:
        if end <= j + len(new):
            return True
        j = content.find(new, j + 1)
    return False


def _count_already_applied(content: str, old: str, new: str) -> int:
    """`replace`'s own version of `_edit_already_applied` (#938), batched.

    `op_edit` only ever has ONE occurrence of `old` in play — more than one
    is refused as ambiguous before this code runs — so a `content.find(new)`
    scan per call was a single O(n) pass. `replace` is replace-all by design
    and can have many occurrences of `old` in one file; calling
    `_edit_already_applied` once per occurrence repeats that O(n) scan for
    EVERY occurrence, which is O(n·m) over a file with m occurrences of a
    short `old` — quadratic in the file's own size once occurrence count
    scales with it, and paid even on a plain first application, since the
    O(n) part runs before the function can tell there is nothing to find.

    Same positional-containment test, answered once per file instead of once
    per occurrence: every occurrence of `old` and every occurrence of `new`
    are each found in one linear pass (`str.find` in a loop, not the
    windowed scan a naive re-implementation would reach for), then `bisect`
    answers "is there a `new` occurrence starting in
    `[old_end - len(new), old_idx]`" — the same interval
    `_edit_already_applied` searches by hand — in O(log k) against the
    ascending list, k being how many times `new` already occurs. Total cost
    O(n + m·log k), not O(n·m).
    """
    if len(new) <= len(old) or old not in new:
        return 0
    old_idxs: List[int] = []
    start = 0
    while True:
        idx = content.find(old, start)
        if idx == -1:
            break
        old_idxs.append(idx)
        start = idx + len(old)
    if not old_idxs:
        return 0
    new_idxs: List[int] = []
    start = 0
    while True:
        idx = content.find(new, start)
        if idx == -1:
            break
        new_idxs.append(idx)
        start = idx + 1
    if not new_idxs:
        return 0
    count = 0
    for old_idx in old_idxs:
        lo = old_idx + len(old) - len(new)
        # The rightmost `new` occurrence at or before `old_idx` is the
        # strongest candidate — a later start covers further right for the
        # same length — so only that one needs checking; if it clears `lo`
        # it satisfies both ends of the interval `_edit_already_applied`
        # searches by hand, and if it does not, no earlier one (a smaller
        # start, the same length) can either.
        pos = bisect.bisect_right(new_idxs, old_idx) - 1
        if pos >= 0 and new_idxs[pos] >= lo:
            count += 1
    return count




def _newline_census(text: str) -> Tuple[int, int, int]:
    """`(crlf, lf, cr)` — how many of each line ending the text actually has.

    Bare counts, not a verdict. Callers that need one say in their own receipt
    what they did with it; nothing here decides on their behalf (#1049).
    """
    crlf = text.count("\r\n")
    lf = text.count("\n") - crlf
    cr = text.count("\r") - crlf
    return crlf, lf, cr


def _newline_used(text: str) -> str:
    """The ending convention a block carries, CRLF-first, "" if it carries none.

    CRLF-first because a block holding both is a caller's own mixture written
    verbatim, and naming the two-byte ending is the one that cannot be read as
    a bare LF.
    """
    for nl in ("\r\n", "\r", "\n"):
        if nl in text:
            return nl
    return ""


def _newline_note(content: str, wrote: str = "", retermed: bool = False) -> str:
    """One receipt line, emitted only where the ending used had more than one
    defensible answer.

    `content` is the file **as it will be on disk** — not as it was found.
    Asking the pre-write bytes meant the mixed branch below could only fire on
    a file that was *already* mixed, so a write that CREATED the mixedness
    reached nothing and shipped in silence (#1075). The census printed with it
    describes the file that now exists, which is the one the reader is about to
    open.

    `wrote` is the convention the op used for text it supplied, "" when it
    supplied none. `retermed` says that text was the *caller's own*, rewritten
    to match the file.

    The first cut of this fired on every successful edit of a CRLF file — where
    nothing had been decided, every byte outside the match was unchanged, and
    the line said so at length. On Windows every file is CRLF, so a marker
    meaning "I made a choice you did not" appeared on every call ever made,
    which is how a disclosure becomes noise and then becomes ignored. It also
    collided with `test_successful_edit_has_no_diagnostic`, which reads `↳` as
    "a diagnostic was emitted" and was right to.

    So: a mixed file has no single answer and is always disclosed; re-writing
    the caller's own endings is always disclosed; supplying a trailing newline
    in the one convention a uniform file uses is not a choice and is silent.
    """
    if not wrote:
        return ""
    names = {"\r\n": "CRLF", "\n": "LF", "\r": "CR"}
    name = names.get(wrote, repr(wrote))
    crlf, lf, cr = _newline_census(content)
    if len([n for n in (crlf, lf, cr) if n]) > 1:
        return (f"  {mark('↳')} line endings: file is mixed ({crlf} CRLF / "
                f"{lf} LF / {cr} CR) — every line this op did not touch kept "
                f"its own; text this op supplied uses {name}\n")
    if not retermed:
        return ""
    return (f"  {mark('↳')} line endings: file is {name}, so the text you "
            f"wrote with LF was re-terminated to {name} to match — every "
            f"untouched line is unchanged\n")


def _newline_variants(text: str) -> List[Tuple[str, str]]:
    """`(newline, text)` candidates for a match, most faithful first.

    A caller composing a payload types LF, so an `old` string describing lines
    of a CRLF file arrives LF-terminated and matches nothing once the read
    stops flattening the file. Refusing there would trade a silent whole-file
    rewrite for a hard `old string not found` — a different bug, not a fix. The
    literal text is therefore tried first and the re-terminated forms only
    after it, so a file that matches exactly is never reinterpreted.
    """
    flat = text.replace("\r\n", "\n").replace("\r", "\n")
    out: List[Tuple[str, str]] = [("", text)]
    for nl in ("\r\n", "\r", "\n"):
        cand = flat.replace("\n", nl)
        if cand != text:
            out.append((nl, cand))
    return out


def _retermed(text: str, nl: str) -> str:
    """`text` with every line ending replaced by `nl`."""
    return text.replace("\r\n", "\n").replace("\r", "\n").replace("\n", nl)


def _line_number_at(text: str, end: Optional[int] = None) -> int:
    """1-based line number of position `end`, counting CR, LF and CRLF alike.

    `text.count("\\n")` is zero at *every* position in a CR-only file, so a
    receipt built on it named line 1 wherever the match actually was. That
    arithmetic was correct only because the read used to translate `\\r` to
    `\\n` before it ran; `newline=""` removed the translation and left the
    count behind (PR #1057 review).

    Counted rather than sliced: `count` takes a range, so this stays
    allocation-free on the dry-run path that calls it once per match.
    """
    return (text.count("\n", 0, end)
            + text.count("\r", 0, end)
            - text.count("\r\n", 0, end)) + 1


def _local_newline(lines: List[str], idx: int) -> str:
    """The line ending in force at `lines[idx]`, scanning backwards for one.

    A mixed file has no single answer, so `replace_lines` does not vote on the
    whole file: the block it writes takes the ending of the line it replaces
    (or, for an insert, of the line above it). A file-wide majority would
    rewrite the caller's own line to the other convention, which is the same
    silent normalisation one line wide.

    The backwards scan is the answer whenever it finds one. When it does not —
    `idx` is at or past the only terminated line, or every line above it is
    unterminated — the search continues *forwards* over the whole file, so a
    file whose endings all sit below `idx` still gets its own convention.
    Falls back to LF only when no line anywhere in the file is terminated.
    """
    for i in range(min(idx, len(lines) - 1), -1, -1):
        line = lines[i]
        for nl in ("\r\n", "\r", "\n"):
            if line.endswith(nl):
                return nl
    for line in lines:
        for nl in ("\r\n", "\r", "\n"):
            if line.endswith(nl):
                return nl
    return "\n"


def op_edit(old: str, new: str, path: str) -> str:
    """Single-file, single-occurrence edit — mirrors native Edit semantics.

    Errors on 0 or >1 matches. For replace_all, use the `replace` op.
    Returns a receipt with ±2 lines of context around the change.
    """
    if not old:
        return "ERROR: empty old string\n"
    if old == new:
        return "ERROR: old and new strings are identical\n"
    if not path:
        return "ERROR: empty path\n"
    if not os.path.isfile(path):
        # Through the shared helper, not a bare f-string. Until #1334 this arm
        # printed the path and nothing else, so `edit` had neither the `tried:`
        # line #1300 added nor the cwd-drift branch — the two things that make
        # naming a creating op safe rather than a guess.
        return _path_not_found(path, label="file", op="edit", creates=True)

    try:
        # surrogateescape: lone bytes that aren't valid UTF-8 round-trip via
        # _atomic_write back to their original byte values. Prevents silent
        # corruption of bytes outside the match window (CVE-class data loss
        # on files with mixed encodings or partial binary content).
        # newline="": no universal-newline translation, so a CRLF file is not
        # silently rewritten to LF throughout — the contract op_append already
        # states. Without it a one-line edit changed every line in the file,
        # under a receipt that named one (#1049).
        with open(path, "r", encoding="utf-8", errors="surrogateescape",
                  newline="") as f:
            content = f.read()
    except OSError as e:
        return f"ERROR: failed to read {path}: {e}\n"

    wrote = ""
    count = content.count(old)
    if count == 0:
        for nl, cand in _newline_variants(old)[1:]:
            n = content.count(cand)
            if n:
                old, new, wrote, count = cand, _retermed(new, nl), nl, n
                break
    if count == 0:
        _bump_counter(_SKIP_COUNT, "cnt_skip")
        return (f"ERROR: old string not found in {path}\n"
                + _edit_miss_diagnostic(old, content, new, path))
    if count > 1:
        _bump_counter(_SKIP_COUNT, "cnt_skip")
        return (
            f"ERROR: old string found {count} times in {path} — ambiguous. "
            f"Use a larger snippet to make it unique, or use replace for "
            f"replace_all semantics.\n"
        )

    retermed = bool(wrote)
    if not wrote and ("\n" in new or "\r" in new):
        # `old` matched literally, so nothing above resolved a convention --
        # but `new` carries one of its own, and writing it verbatim splices LF
        # into a CRLF file and leaves it mixed, silently.
        #
        # The defect this closes is an inconsistency, not just a silence: an
        # `old` that spanned a line boundary went through `_newline_variants`
        # and had `new` re-terminated with whatever matched, while a
        # single-line `old` did not. Two edits expressing the same intent
        # produced different bytes depending on that accident (PR #1057
        # review). Only reachable since this branch stopped flattening every
        # write to LF, which is why it is this branch's regression to fix.
        crlf, lf, cr = _newline_census(content)
        used = [n for n, c in (("\r\n", crlf), ("\n", lf), ("\r", cr)) if c]
        if len(used) == 1 and _retermed(new, used[0]) != new:
            # One convention, and `new` disagrees: match the file and say so.
            # This is the caller's own text rewritten, which `_newline_note`
            # promises is never silent.
            new, wrote, retermed = _retermed(new, used[0]), used[0], True
        elif len(used) > 1:
            # Mixed: no single convention to match, so the caller's bytes stand
            # as typed -- the same answer `replace_lines` gives. Still a
            # decision, and `_newline_note`'s mixed branch discloses it.
            wrote = _newline_used(new)

    idx = content.index(old)
    reapplied = _edit_already_applied(content, old, new, idx)

    new_content = content.replace(old, new, 1)
    try:
        _atomic_write(path, new_content)
    except OSError as e:
        return f"ERROR: failed to write {path}: {e}\n"

    if reapplied:
        _bump_counter(_REAPPLY_COUNT, "cnt_reapply")

    # Receipt — locate the change and show ±2 lines context
    start_line = _line_number_at(content, idx)
    # `_line_number_at` counts LF/CR/CRLF; the receipt's own line list has to
    # count the same ones or the context block is indexed against a numbering
    # the `line N` above it was never built from (#1060).
    new_lines = _split_lines(new_content)
    new_block_line_count = _line_number_at(new)
    end_line = start_line + new_block_line_count - 1
    ctx_start = max(1, start_line - 2)
    ctx_end = min(len(new_lines), end_line + 2)

    # The path is flattened for the same reason the op header above it is
    # (#1019): this line is the receipt's own success claim, and a name
    # carrying a line separator put `[rolled back] …` at column 0 underneath
    # it — a retraction the reader's anchored grep believes, for a file that
    # was in fact written. Disclosed, never stripped: `_flat_field` leaves the
    # name readable in full so the operator can still go and look at it.
    out = [f"edited {_flat_field(path, disclose_newline=True)} (line {start_line}"]
    if end_line != start_line:
        out.append(f"-{end_line}")
    out.append(")\n")
    _nl_note = _newline_note(new_content, wrote, retermed=retermed)
    if _nl_note:
        out.append(_nl_note)
    # Attached to the claim it qualifies, not only to the footer: `edited a.py
    # (line 2-3)` is a true sentence that reads as a first application, and the
    # reader who is about to trust it is looking here. The footer carries the
    # same signal for the reader who is piping to `tail` (#621).
    if reapplied:
        out.append(
            f"  {mark('↳')} re-applied: the text this edit produces was already "
            f"present around the anchor — this is a SECOND application, not a "
            f"repeat of the first\n"
        )
    for ln in range(ctx_start, ctx_end + 1):
        marker = "→" if start_line <= ln <= end_line else " "
        out.append(f"  {ln:>5} {marker} {new_lines[ln - 1]}\n")
    return "".join(out)


def op_json_set(path: str, fields: Dict[str, Any]) -> str:
    """Set one or more fields in a JSON file by dotted key path, in one write
    (#1822).

    Closes the gap between `paste` (whole file) and `edit` (exact-string,
    single occurrence): a JSON report with ~100 fields where 8 changed had
    no proportional route — every re-send was the full 22 KB. This op takes
    a small `fields` mapping (`{"tests.green.result": "..."}`) and does the
    whole-file work internally: parse, set each leaf on the in-memory
    document, re-serialize.

    Never guesses at structure. Three refusals, none of them silent:
      - the file does not parse as JSON at all — this is not a text patch,
        it needs a real document to walk;
      - a dotted path's leading segment does not resolve to an existing
        object — `json-set` sets fields, it does not fabricate the objects
        that would contain them (use `paste` for a new structure);
      - a value TOML can produce that JSON cannot represent (a date/time
        literal) — refused before anything is written, not coerced.

    Because the write is always a full valid re-serialization of a
    document this function itself parsed, a well-formed call cannot land
    syntactically invalid JSON on disk — unlike `edit`/`vim`'s raw text
    patches, which is why `edit` needs the same-shape rollback guarantee
    and this op inherits it structurally. It is still routed through
    `_run_with_validators`/`jsonlint` (`rollback_on_fail: true`) like every
    other write op, so a validator this repo has not yet configured — a
    schema check, say — gets the identical safety net for free.
    """
    if not path:
        return "ERROR: empty path\n"
    if not fields:
        return "ERROR: empty set — nothing to change\n"
    if not os.path.isfile(path):
        return _path_not_found(path, label="file", op="json-set")
    try:
        with open(path, "r", encoding="utf-8") as f:
            raw = f.read()
    except OSError as e:
        return f"ERROR: failed to read {path}: {e}\n"
    try:
        doc = json.loads(raw)
    except json.JSONDecodeError as e:
        return (f"ERROR: {path} does not parse as JSON — json-set refuses to "
                f"guess at a document it cannot read: {e}\n")
    if not isinstance(doc, dict):
        return (f"ERROR: {path}'s top level is a {type(doc).__name__}, not a "
                f"JSON object — json-set only sets fields inside an object\n")

    changes: List[Tuple[str, bool, Any, Any]] = []
    for dotted_key, new_value in fields.items():
        if not isinstance(dotted_key, str) or not dotted_key:
            return (f"ERROR: json-set field name must be a non-empty string, "
                     f"got {dotted_key!r}\n")
        segs = dotted_key.split(".")
        if any(not s for s in segs):
            return (f"ERROR: {dotted_key!r} is not a valid dotted field path "
                     f"(empty segment)\n")
        cur: Any = doc
        walked: List[str] = []
        for seg in segs[:-1]:
            if not isinstance(cur, dict) or seg not in cur:
                return (
                    f"ERROR: {dotted_key!r} — "
                    f"{'.'.join(walked + [seg])} does not exist in {path}; "
                    f"json-set will not create missing intermediate objects. "
                    f"Use paste for a new structure.\n"
                )
            cur = cur[seg]
            walked.append(seg)
        leaf = segs[-1]
        if not isinstance(cur, dict):
            return (
                f"ERROR: {dotted_key!r} — {'.'.join(walked) or '(top level)'} "
                f"in {path} is a {type(cur).__name__}, not an object; cannot "
                f"set a field inside it\n"
            )
        existed = leaf in cur
        old_value = cur.get(leaf)
        cur[leaf] = new_value
        changes.append((dotted_key, existed, old_value, new_value))

    try:
        new_content = json.dumps(doc, indent=2, ensure_ascii=False) + "\n"
    except (TypeError, ValueError) as e:
        return f"ERROR: a value for json-set is not JSON-serializable: {e}\n"

    try:
        _atomic_write(path, new_content)
    except OSError as e:
        return f"ERROR: failed to write {path}: {e}\n"

    out = [f"set {len(changes)} field(s) in "
           f"{_flat_field(path, disclose_newline=True)}\n"]
    for dotted_key, existed, old_value, new_value in changes:
        verb = "changed" if existed else "added"
        old_disp = json.dumps(old_value) if existed else "(absent)"
        new_disp = json.dumps(new_value)
        out.append(f"  {dotted_key}: {old_disp} -> {new_disp} ({verb})\n")
    return "".join(out)


_PASTE_BACKUP_KIND = "paste-backup"

# A copy on every overwrite is a resource claim, so it is bounded — and the
# bound is NOT the trigger #1650 argued about. It does not decide whether the
# outgoing bytes are worth keeping; it decides how much disk supertool is
# willing to spend keeping them. Which is exactly why crossing it is said out
# loud in the receipt: it lands on the largest files, where the loss is worst,
# and an unbacked overwrite that reads like a backed one is this repo's
# recurring defect. 8 MB clears every text file this tool is aimed at —
# `_supertool.py` is 1.2 MB and `CHANGELOG.md` 1.8 MB, the two largest files
# in this tree.
_PASTE_BACKUP_MAX_BYTES = 8 * 1024 * 1024


def _paste_snapshot(path: str, new_content: str) -> Tuple[str, str]:
    """Copy `path`'s current bytes aside before `paste` replaces them (#1650).

    Returns `(snapshot_path, why_not)` — three states, not two. `("/x", "")`
    the bytes are kept and here is where; `("", "")` there was nothing to keep,
    because the file already holds exactly what is about to be written; and
    `("", why)` the copy could not be made, which the receipt has to say out
    loud. A store that fails silently is this repo's recurring defect wearing a
    backup's clothes.

    Why a copy and not a refusal. `paste` over an existing file is documented,
    ordinary and usually right, so a guard that stops it has to offer a `force`
    token, and a `force` token that gets typed by reflex is the guard deleting
    itself (the `misdirects` class). A copy refuses nothing, so there is no
    case it was not written for: the write the caller asked for happens either
    way, and the only cost of a false positive is one reaped cache file.

    Why `paste` alone. Every other mutating op fails on a path that is not
    there: `edit` and `replace` match a string, and `vim` and `replace_lines`
    both return `file not found`. Only `paste` succeeds either way. That is the
    whole of the claim — NOT that nothing else destroys bytes, because plenty
    does: `vim` empties a file completely with `ggdG`, and `replace_lines`
    clamps an end of `total + 1` rather than refusing it, so `1:total+1` takes
    the file down to one block. What none of them can do is destroy a file the
    caller believes is not there, and that belief is the #1642 mechanism: 8922
    bytes overwritten by an agent creating what it thought was a new note.

    Why no size threshold. #1650 proposes a shrink ratio or a byte-loss floor.
    Both are blind to 8922 bytes replaced by 9000 different ones, which loses
    just as much. `paste` replaces the entire file by definition, so the
    trigger is the op's own semantics and there is no number to tune.
    """
    try:
        with open(path, "rb") as fh:
            old = fh.read()
    except OSError as e:
        return "", f"could not read the outgoing bytes ({type(e).__name__})"
    if old == new_content.encode("utf-8", errors="surrogateescape"):
        # Nothing is lost, so nothing is stored, and the receipt stays quiet.
        return "", ""
    if len(old) > _PASTE_BACKUP_MAX_BYTES:
        return "", (
            f"{len(old)} bytes is over the {_PASTE_BACKUP_MAX_BYTES}-byte "
            f"copy limit"
        )
    # The mode the copy has to land at, decided BEFORE the store is touched
    # (#1685). A bare `open(dest, "wb")` writes at `0666 & ~umask` — 0644 on a
    # stock box — whatever the source was, so a `paste` over a 0600 `.env`,
    # `id_rsa` or `.netrc` left the secret group- and world-readable under
    # `~/.cache/supertool` for the whole seven-day retention window. The copy
    # is not redacted, correctly, because it is a backup; what has to hold is
    # that reading the copy is no easier than reading the original.
    #
    # Three states, not two. A mode that could not be read is not a mode of
    # 0644: the fallback is owner-only, which is never wider than whatever the
    # source turns out to have been. The opposite trade — declining to snapshot
    # a mode-restricted file — would close the disclosure by deleting the
    # data-loss net #1650 exists to be, which is choosing a different failure
    # rather than removing one.
    try:
        snap_mode = stat.S_IMODE(os.stat(path).st_mode)
    except OSError:
        snap_mode = 0o600
    store = _cache_root() / _PASTE_BACKUP_KIND
    # Flat, and a regular file: `_gc_sweep_kind` is non-recursive and unlinks
    # nothing else, so a snapshot in a subdirectory would be counted `skipped`
    # forever — a reaped-looking store that only grows.
    dest = store / "{}-{}.bak".format(
        hashlib.sha1(
            os.path.abspath(path).encode("utf-8", errors="surrogateescape")
        ).hexdigest()[:16],
        time.time_ns(),
    )
    # `mkdir(exist_ok=True)` succeeds on a symlink that points at a directory,
    # and the write that follows lands wherever it points. The `time_ns()` leaf
    # is unpredictable, so nobody can name the file in advance — but the
    # directory is a fixed name under a shared cache root, so one planted
    # symlink redirects every later snapshot. Refused out loud rather than
    # followed, which keeps it a declared `why_not` instead of a copy the
    # receipt claims is somewhere it is not.
    #
    # This defends the leaf only. A symlink at `_cache_root()` itself, or the
    # `XDG_CACHE_HOME` read that chooses it with no shape check, is the same
    # class one level up and is deliberately not fixed here (#1685 item 2).
    if store.is_symlink():
        return "", f"{store} is a symlink, not a directory"
    # That check alone is check-then-act, so it is the message and not the
    # guarantee: where the platform has them, the store is then *opened* with
    # O_DIRECTORY | O_NOFOLLOW and the leaf is created relative to that
    # descriptor, which refuses a symlink swapped in after the check rather
    # than resolving through it. Windows has neither, and no POSIX mode bits
    # to disclose either, so it keeps the path-based open.
    use_dir_fd = hasattr(os, "O_DIRECTORY") and os.open in os.supports_dir_fd
    dir_fd = None
    try:
        store.mkdir(parents=True, exist_ok=True)
        if use_dir_fd:
            dir_fd = os.open(
                str(store), os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
            )
        # os.open, not open(): the mode has to be on the file from the moment
        # it exists rather than chmod-ed onto it a syscall later, or the bytes
        # are world-readable for that window. O_EXCL so an existing leaf is
        # never written through, O_NOFOLLOW so a symlink at the leaf is not
        # either, and O_BINARY because on Windows a text-mode fd would rewrite
        # every LF in the backup — a copy that does not match what it copied.
        flags = (
            os.O_WRONLY | os.O_CREAT | os.O_EXCL
            | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0)
        )
        fd = os.open(
            dest.name if dir_fd is not None else str(dest),
            flags,
            snap_mode,
            dir_fd=dir_fd,
        )
        with os.fdopen(fd, "wb") as fh:
            # The umask subtracts from the mode passed to os.open, so a 0640
            # source under a 0022 umask would land at 0620 — narrower, but a
            # different fact from the one the receipt states. On the open
            # descriptor and before the bytes, never `os.chmod(dest, ...)`
            # after the handle is closed: a chmod by path re-resolves the name
            # and would hand back the symlink window O_NOFOLLOW just shut.
            if hasattr(os, "fchmod"):
                os.fchmod(fh.fileno(), snap_mode)
            fh.write(old)
    except OSError as e:
        return "", f"{store} is not writable ({type(e).__name__})"
    finally:
        if dir_fd is not None:
            os.close(dir_fd)
    return str(dest), ""


def _snapshot_mode_suffix(snapshot: str) -> str:
    """What the receipt says about the mode of a `paste` snapshot (#1685).

    `_created_mode_note` states the mode of a file `paste` has just created,
    because that is a new fact about a new file. This is the other half and it
    is a weaker claim: the snapshot is a copy of bytes the caller already owns,
    so the mode is not news — what is news is that a second copy of those bytes
    now exists somewhere else, and whether reading it is any easier than
    reading the original. So it rides on the line that already names the copy
    rather than taking one of its own.

    Read back off disk, never from the mode that was requested: `os.open`'s
    mode argument is subtracted by the umask, and a receipt that quoted the
    request would be stating a permission the file may not have. If the stat
    fails the suffix is empty — the copy exists and its mode is unknown, and
    there is no fact to disclose. Nothing is printed on Windows, for the same
    reason `_created_mode_note` prints nothing there: `st_mode` reads back
    0o666 whatever was asked for.
    """
    if os.name == "nt":
        return ""
    try:
        mode = stat.S_IMODE(os.stat(snapshot).st_mode)
    except OSError:
        return ""
    return " (mode {0:04o}, the file's own)".format(mode)


def _created_mode_note(path: str, content: str) -> str:
    """What `paste` says about the mode of a file it has just created (#1275).

    It states the mode and stops there. `paste` does NOT infer the executable
    bit — not from the shebang, not from the modes of the file's neighbours,
    not from an `+x` flag — because each of those is a guess whose wrong answer
    is silent in one direction and destructive in the other: an inferred `+x`
    on a payload-written file is a permission nobody asked for, and a missing
    one is a script that does not run. Disclosure has no wrong answer, and the
    caller already knows which of the two they wrote.

    What it does add, for the case that produced the issue, is the fact and the
    one command: content beginning `#!` is a script, and a script without an
    executable bit is a broken artefact whose first symptom arrives after the
    merge. Naming `chmod +x` is not inference — the mode on disk is unchanged
    either way.

    **Nothing is printed on Windows.** There is no executable bit there and
    `os.chmod` honours only the read-only flag, so `st_mode` reads back 0o666
    whatever was asked for. A mode line would be noise at best and a false
    statement about the file's access at worst.
    """
    if os.name == "nt":
        return ""
    try:
        mode = stat.S_IMODE(os.stat(path).st_mode)
    except OSError:
        # The write succeeded and the stat did not, so the mode is unknown.
        # Saying nothing is right here: this note exists to disclose a fact,
        # and there is no fact to disclose.
        return ""
    note = "  ↳ mode {0:04o}, from the process umask\n".format(mode)
    if content.startswith("#!") and not (mode & 0o111):
        note += (
            "  ↳ starts with `#!` but is not executable — "
            "`chmod +x {0}` to run it\n".format(path)
        )
    return note


def op_paste(path: str, content: str) -> str:
    """Replace entire file with content. Atomic. Creates file (and parent dirs)
    if missing.

    Overwriting an EXISTING file copies its outgoing bytes to
    `~/.cache/supertool/paste-backup/` first and names the copy in the receipt
    — see `_paste_snapshot` for why that is a copy and not a refusal (#1650).
    The copy carries the source's own mode, so a 0600 file does not get a
    world-readable backup, and the receipt states the mode it landed at
    (#1685).

    Use for full-file rewrites — no vim macro gymnastics, no `:r` insert-after
    off-by-one cuts (e.g. `<?php` eaten), no `:::` separator abuse. CONTENT
    arrives via triple-colon separator so it can hold any chars (`:`, quotes,
    braces, newlines).
    """
    if not path:
        return "ERROR: empty path\n"
    # Containment check BEFORE makedirs — otherwise a path like
    # `../../tmp/evil/foo` would create directories outside cwd before
    # _atomic_write's own _safe_path check rejects the write itself.
    try:
        safe_resolved = _safe_path(path)
    except SecurityError as e:
        return f"ERROR: {e}\n"
    parent = os.path.dirname(safe_resolved)
    if parent and not os.path.isdir(parent):
        try:
            os.makedirs(parent, exist_ok=True)
        except OSError as e:
            return f"ERROR: failed to create parent dir {parent}: {e}\n"
    # Ensure trailing newline (POSIX text files)
    if content and not content.endswith("\n"):
        content += "\n"
    existed = os.path.isfile(path)
    old_size = os.path.getsize(path) if existed else 0
    # Snapshot BEFORE the write, because the receipt cannot help after it
    # (#1650). A create has no prior bytes, so it pays nothing.
    snapshot_note = ""
    if existed and old_size > 0:
        snap, why = _paste_snapshot(path, content)
        if snap:
            snapshot_note = (
                f"  ↳ previous contents kept at {snap}"
                + _snapshot_mode_suffix(snap)
                + "\n"
            )
        elif why:
            snapshot_note = (
                f"  ↳ no backup of the previous contents — {why}\n"
            )
    try:
        _atomic_write(path, content)
    except OSError as e:
        return f"ERROR: failed to write {path}: {e}\n"
    new_size = len(content.encode("utf-8"))
    new_lines = content.count("\n")
    verb = "rewrote" if existed else "created"
    return (
        f"{verb} {path} ({new_lines} lines, {old_size} → {new_size} bytes)\n"
        + snapshot_note
        + ("" if existed else _created_mode_note(path, content))
    )


_APPEND_RECEIPT_LINES = 10


def op_append(path: str, content: str) -> str:
    """Append content to the end of a file. Atomic. Creates it if missing.

    Appending used to need two calls: `wc:PATH` to learn the line count, then
    `replace_lines` with `start = N+1, end = N` — the inverted-range insert
    form. That is a round-trip spent computing an argument, and `887:886` reads
    like a typo to whoever reviews the command later.

    A file whose last line has no trailing newline gets one first, so the
    appended block always starts on its own line; the receipt says so, since
    silently touching a byte the caller did not ask about is worth a word.
    """
    if not path:
        return "ERROR: empty path\n"
    if not content:
        return "ERROR: empty content — nothing to append\n"
    # Containment check BEFORE makedirs, same ordering as op_paste: a path like
    # `../../tmp/evil/foo` must not create directories outside cwd.
    try:
        safe_resolved = _safe_path(path)
    except SecurityError as e:
        return f"ERROR: {e}\n"
    if os.path.isdir(path):
        # Explicit, unlike op_paste which lets _atomic_write's OSError surface.
        # `append` is the op most likely to be aimed at a directory by mistake
        # — a notes/ or logs/ path with the filename left off — and saying so
        # beats an mkstemp errno.
        return f"ERROR: {path} is a directory\n"
    parent = os.path.dirname(safe_resolved)
    if parent and not os.path.isdir(parent):
        try:
            os.makedirs(parent, exist_ok=True)
        except OSError as e:
            return f"ERROR: failed to create parent dir {parent}: {e}\n"

    existed = os.path.isfile(path)
    orig = ""
    if existed:
        try:
            # surrogateescape: lone non-UTF-8 bytes round-trip through
            # _atomic_write instead of being mangled to U+FFFD — same contract
            # as op_edit / op_replace_lines.
            # newline="": no universal-newline translation, so a CRLF file is
            # not silently rewritten to LF throughout. An append touches the
            # end of the file; every other byte must come back out unchanged.
            with open(path, "r", encoding="utf-8", errors="surrogateescape",
                      newline="") as f:
                orig = f.read()
        except OSError as e:
            return f"ERROR: failed to read {path}: {e}\n"

    # The file's own convention, scanned backwards from EOF -- the same
    # `_local_newline` helper `replace_lines` uses for the terminator it has
    # to invent (#1073/#1075). Falls back to LF when the file does not exist
    # or has no terminated line to read a convention off.
    orig_lines = _split_lines_keepends(orig) if orig else []
    conv = _local_newline(orig_lines, len(orig_lines) - 1) if orig_lines else "\n"

    newline_hint = ""
    if orig and not orig.endswith(("\n", "\r")):
        # Match the file's own convention rather than imposing a file-wide
        # majority guess -- same helper as above, so this and the block's own
        # invented terminator never disagree.
        orig += conv
        newline_hint = " [added the missing trailing newline first]"

    # The caller's own text goes in verbatim, endings and all -- rewriting an
    # explicit choice would be the same silent normalisation pointed the other
    # way (see `replace_lines`'s identical contract). The one ending this op
    # *invents* is the trailing one, when content carries none at all; that
    # takes the file's convention, not a hardcoded LF (#1085).
    block = content if content.endswith(("\n", "\r")) else content + conv

    old_size = len(orig.encode("utf-8", errors="surrogateescape")) if existed else 0
    new_content = orig + block
    try:
        _atomic_write(path, new_content)
    except OSError as e:
        return f"ERROR: failed to write {path}: {e}\n"

    all_lines = _split_lines(new_content)
    # Not `block.count("\n")`: that undercounts (or zeroes) whenever `block`
    # is terminated with the file's own CR-only convention -- a leftover from
    # when this was always LF-terminated, so counting "\n" was always exact.
    # `_split_lines_keepends` is the same line definition the receipt below
    # and `all_lines` above are already built from (#1085).
    added = len(_split_lines_keepends(block))
    start_line = len(all_lines) - added + 1
    new_size = len(new_content.encode("utf-8", errors="surrogateescape"))
    verb = "appended to" if existed else "created"
    out = [
        f"{verb} {path}: {added} lines at {start_line}-{len(all_lines)} "
        f"({old_size} → {new_size} bytes){newline_hint}\n"
    ]
    # Computed from the file as written, not as it was found -- the same
    # post-write basis #1075 fixed `replace_lines` onto, and for the same
    # reason: the pre-write bytes can never show mixedness this write itself
    # created (#1085).
    _nl_note = _newline_note(new_content, _newline_used(block))
    if _nl_note:
        out.append(_nl_note)
    if not existed:
        # `append` is the other op that brings a file into existence, so the
        # umask default of #1275 reaches it through the same chokepoint — and
        # a widening nobody is told about is the exact shape that fix removes.
        # Disclosed here rather than only in `paste`.
        out.append(_created_mode_note(path, new_content))
    # Receipt shows 2 lines of preceding context so the caller can see what the
    # block landed after, then the block itself — capped, because append is the
    # op you reach for with a long changelog entry and echoing it back in full
    # is pure token cost on content the caller already had.
    ctx_start = max(1, start_line - 2)
    shown_end = min(len(all_lines), start_line + _APPEND_RECEIPT_LINES - 1)
    for ln in range(ctx_start, shown_end + 1):
        marker = "→" if ln >= start_line else " "
        out.append(f"  {ln:>5} {marker} {all_lines[ln - 1]}\n")
    if shown_end < len(all_lines):
        out.append(f"  … (+{len(all_lines) - shown_end} more appended lines)\n")
    return "".join(out)


def op_replace_lines(path: str, start: int, end: int, content: str) -> str:
    """Replace lines [start, end] (1-indexed, inclusive) with content.

    Modes (all via the same op):
      insert  — end < start (e.g. 42:41) inserts CONTENT before line `start`
      replace — end >= start, swap range with CONTENT
      delete  — empty CONTENT, removes lines in range

    Returns receipt with new line numbers + ±2 context.
    """
    if not path:
        return "ERROR: empty path\n"
    if not os.path.isfile(path):
        return _path_not_found(path, label="file", op="replace_lines",
                               creates=True)
    if start < 1:
        return f"ERROR: start ({start}) must be >= 1\n"
    if end < 0:
        return f"ERROR: end ({end}) must be >= 0\n"

    try:
        # surrogateescape (not 'replace'): round-trip lone non-UTF-8 bytes
        # via _atomic_write. 'replace' would silently mutate them to U+FFFD
        # in untouched regions — same bug fix as op_edit / op_replace.
        # newline="": the lines this op does not name must come back out byte
        # for byte, CRLF included (#1049).
        with open(path, "r", encoding="utf-8", errors="surrogateescape",
                  newline="") as f:
            orig = f.read()
    except OSError as e:
        return f"ERROR: failed to read {path}: {e}\n"

    # The shared definition (#1060): `str.splitlines` breaks on eight more
    # characters than the byte-level split `read` renders with, so a file
    # holding one of them was numbered one way for the reader and another way
    # for this write. Both sides go through `_split_lines_keepends` now.
    orig_lines = _split_lines_keepends(orig)
    total = len(orig_lines)

    if start > total + 1:
        return f"ERROR: start ({start}) > file length ({total}) + 1\n"

    insert_only = end < start
    # Off-by-one autocorrect: END == total + 1 — Kevin guessed line count, clearly
    # meant "through EOF". Clamp + flag in receipt. END > total+1 = real mistake.
    clamped_hint = ""
    if not insert_only and end == total + 1:
        clamped_hint = f" [autocorrect: end ({end}) clamped to file length ({total})]"
        end = total
    if not insert_only and end > total:
        return f"ERROR: end ({end}) > file length ({total})\n"

    # The caller's content goes in verbatim, mixed endings and all: the
    # endings inside the block they typed are their choice, and rewriting an
    # explicit choice is the same silent normalisation #1049 is about pointed
    # the other way. `test_mixed_line_endings_preserved` states this contract
    # and predates the issue.
    #
    # The one ending this op has to *invent* is the trailing one, when the
    # block does not end a line at all. That takes the ending of the line it
    # lands on — not LF and not a file-wide majority, which on a mixed file
    # would rewrite the caller's own line to the other convention.
    block_nl = _local_newline(orig_lines, start - 1)
    new_block = content
    if new_block and not new_block.endswith(("\n", "\r")):
        new_block += block_nl
    new_block_lines = _split_lines_keepends(new_block) if new_block else []

    if insert_only:
        before = orig_lines[: start - 1]
        after = orig_lines[start - 1:]
        removed = 0
    else:
        before = orig_lines[: start - 1]
        after = orig_lines[end:]
        removed = end - start + 1

    new_lines = before + new_block_lines + after
    new_content = "".join(new_lines)
    try:
        _atomic_write(path, new_content)
    except OSError as e:
        return f"ERROR: failed to write {path}: {e}\n"

    added = len(new_block_lines)
    new_start = start
    new_end = start + added - 1 if added > 0 else start - 1

    if added == 0:
        verb = f"deleted lines {start}-{end}"
    elif insert_only:
        verb = f"inserted {added} lines before line {start}"
    else:
        verb = f"replaced lines {start}-{end} with lines {new_start}-{new_end}"
    out = [f"{verb} in {path} (Δ {added - removed:+d}){clamped_hint}\n"]
    # The block as written, not merely the ending this op had to invent: a
    # block that already ended a line invented nothing, left `wrote` empty, and
    # short-circuited the note before it ever reached the census — the same
    # mixed file under the same silence (#1075).
    _nl_note = _newline_note(new_content,
                             _newline_used(new_block) if added else "")
    if _nl_note:
        out.append(_nl_note)

    ctx_start = max(1, new_start - 2)
    ctx_end = min(len(new_lines), max(new_end, new_start) + 2)
    for ln in range(ctx_start, ctx_end + 1):
        marker = "→" if added > 0 and new_start <= ln <= new_end else " "
        # rstrip("\r\n"), not rstrip("\n"): on a CRLF file the latter leaves
        # the carriage return inside the receipt, shipping a stray CR into the
        # caller's terminal and logs on every context line (PR #1057 review).
        text = new_lines[ln - 1].rstrip("\r\n")
        out.append(f"  {ln:>5} {marker} {text}\n")
    return "".join(out)
