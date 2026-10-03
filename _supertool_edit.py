

























from __future__ import annotations

if "_load_part" not in globals():
    raise ImportError(
        "_supertool_edit.py is a part of _supertool, loaded via "
        "_load_part() (#2706) -- it cannot be imported directly. Run "
        "supertool.py, or `import _supertool` instead."
    )

def op_replace(old: str, new: str, path: str = ".", dry: bool = False) -> str:










    if not old:
        return "ERROR: empty search pattern\n"
    if old == new:
        return "ERROR: old and new strings are identical\n"
    if not path:
        return "ERROR: empty path\n"


    if path != "." and not os.path.isfile(path) and not os.path.isdir(path):
        return _path_not_found(path, op="replace", creates=True)

    candidates = _grep_candidates(path, _get_exclude_paths("replace"))
    if not candidates:
        return "(0 files to search)\n"









    file_matches: List[Tuple[str, List[int], str, str]] = []
    total_count = 0
    for file_path in candidates:
        try:
            with open(file_path, "rb") as f_bin:
                head = f_bin.read(4096)
            if b"\x00" in head:



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



        if not dry:
            _bump_counter(_SKIP_COUNT, "cnt_skip")
        return f"(0 occurrences of '{old}' found)\n"

    if dry:
        out: List[str] = [f"({total_count} occurrences in {len(file_matches)} files)\n"]








        retermed: Dict[str, str] = {}
        for filepath, positions, eff_old, eff_new in file_matches:



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



            out.append(f"\n  {mark('↳')} line endings: the text you supplied "
                       f"does not match {len(retermed)} of these files byte "
                       f"for byte and would be re-terminated to the "
                       f"convention marked above to make it match — every "
                       f"untouched line would be unchanged\n")
        out.append(f"\nSummary: {total_count} replacements in {len(file_matches)} files (DRY RUN — no files modified)\n")
        return "".join(out)


    files_modified: Dict[str, int] = {}
    vanished: List[str] = []









    reapplied_counts: Dict[str, int] = {}
    for file_path, _scan_positions, eff_old, eff_new in file_matches:
        try:





            with open(file_path, "r", encoding="utf-8",
                      errors="surrogateescape", newline="") as f:
                content = f.read()
        except OSError:
            continue



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
















    retermed: Dict[str, str] = {}
    for _fp, _positions, _eff_old, _eff_new in file_matches:
        if _fp in files_modified and _eff_old != old:
            retermed[_fp] = ("CRLF" if "\r\n" in _eff_old
                             else "CR" if "\r" in _eff_old else "LF")




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




        out.append(f"  {mark('↳')} line endings: the text you supplied did not "
                   f"match {len(retermed)} of these files byte for byte and "
                   f"was re-terminated to the convention marked above to make "
                   f"it match — every untouched line is unchanged\n")
    if vanished:


        out.append(f"\n{len(vanished)} file(s) matched during the scan and no "
                   f"longer matched when the write read them back — NOT "
                   f"modified:\n")
        for fp in sorted(vanished):
            out.append(f"  {_fwd(fp)}\n")
    out.append(f"\nDone: '{old}' → '{new}'\n")
    return "".join(out)


def _write_target(path: str) -> str:








    return os.path.realpath(path) if os.path.islink(path) else path


def _write_target_display(path: str) -> str:





















    target = _write_target(path)
    if target == path:
        return target
    path_dir = os.path.dirname(os.path.abspath(path))
    target_dir = os.path.dirname(target)
    if path_dir != target_dir and os.path.realpath(path_dir) == os.path.realpath(target_dir):
        return os.path.join(path_dir, os.path.basename(target))
    return target


def _process_umask() -> int:







    cur = os.umask(0o022)
    os.umask(cur)
    return cur










_NEW_FILE_MODE = 0o666 & ~_process_umask()


def _atomic_write(path: str, content: str) -> None:



















    import tempfile


    _safe_path(path)




    _pinned = getattr(_DISPATCH_STATE, "pinned_write_target", None)
    _use_pin = _pinned is not None and _pinned[0] == path



    path = _expand_home(path)





    _path_meta_bulk_drop()


    _warn = _sh_backslash_warning(path, content)
    _key = os.path.abspath(path)





    real_path = _pinned[1] if _use_pin else _write_target(path)
    target_dir = os.path.dirname(os.path.abspath(real_path)) or "."















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




    _bump_counter(_WRITE_COUNT, "cnt_write")

    _drop_write_warnings(path)
    if _warn:
        _WRITE_WARNINGS.append((_key, _warn))




    _py_advice = _PAYLOAD_PY_ESCAPE_ADVISORY.pop(_key, "")
    if _py_advice:
        _WRITE_WARNINGS.append((_key, _py_advice))


_BRANCH_CACHE: List[Optional[Tuple[str, str]]] = [None]



_EDIT_DIAG_MAX_LINES = 20000

_GIT_TIMEOUT_DEFAULT = 5


def _git_timeout() -> int:







    return _env_int(os.environ.get("SUPERTOOL_GIT_TIMEOUT"), "SUPERTOOL_GIT_TIMEOUT", _GIT_TIMEOUT_DEFAULT, minimum=1)


def _branch_reading() -> Tuple[str, str]:





















    if _BRANCH_CACHE[0] is None:
        branch = ""
        why = ""
        budget = _git_timeout()
        try:




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





    return _branch_reading()[0]


def _branch_line() -> str:






    branch, why = _branch_reading()
    if branch:
        return f"[branch: {branch}]\n"


    if why:
        return f"[branch: UNKNOWN — {why}]\n"
    return ""


def _result_line(ops: int, writes: int, skipped: int = 0,
                 reapplied: int = 0,
                 not_checked: Optional[Sequence[str]] = None,
                 rolled_back: int = 0,
                 validated: Optional[Sequence[Tuple[str, bool, bool]]] = None,
                 left_on_disk: int = 0) -> str:






















































































































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







    hints: List[str] = []





















    if new and new.strip() and new in content:
        at = content.count("\n", 0, content.index(new)) + 1
        hints.append(
            f"the replacement text is ALREADY present at line {at} — this "
            f"may be a re-run of an edit that already applied, but it could "
            f"also be a coincidental match; if line {at} is not where you "
            f"meant to edit, the anchor is likely just missing"
        )









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



    if not hints:
        near = _edit_nearest_hint(old, lines, path)
        if near:
            hints.append(near)

    return "".join(f"  {mark('↳')} {h}\n" for h in hints)



_EDIT_NEAR_FLOOR = 0.6



_EDIT_NEAR_TIE = 0.02



_EDIT_NEAR_WINDOWS = 20







_EDIT_NEAR_MAX_CHARS = 1000
_EDIT_NEAR_BUDGET = 20_000_000


def _nearest_clip(text: str) -> str:
    return text[:_EDIT_NEAR_MAX_CHARS]


def _nearest_line_candidates(
        needle: str,
        lines: List[str]) -> Tuple[List[Tuple[float, int]], float, bool]:













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
        start = i - n + 2  
        if matched > best_hits:
            best_hits, tops, top_count = matched, [start], 1
        elif matched == best_hits and matched > 0:
            top_count += 1
            if len(tops) < _EDIT_NEAR_WINDOWS:
                tops.append(start)
    if best_hits <= 0:




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





    floor = top_count if top_count > len(tops) and len(near) == len(tops) else 0
    return near, best, False, floor


def _nearest_pct(ratio: float, identical: bool) -> str:













    if not identical and round(ratio * 100) >= 100:
        return ">99%"
    return f"{ratio:.0%}"


def _nearest_diff_kind(anchor: List[str], window: List[str]) -> str:























    if anchor == window:
        return ("identical line-for-line, so the difference is outside the "
                "compared text: blank or whitespace-only lines at the ends of "
                "`old`, or line endings")
    if [_normalise_ws(ln) for ln in anchor] == [_normalise_ws(ln) for ln in window]:
        return "differs only in whitespace"
    if len(anchor) == 1:




        return ""
    same = sum(block.size for block
               in difflib.SequenceMatcher(a=anchor, b=window,
                                          autojunk=False).get_matching_blocks())







    differing = max(1, len(anchor) - same)













    if differing == 1 and len(anchor) == len(window):
        _diff_at = [i for i in range(len(anchor)) if anchor[i] != window[i]]
        if len(_diff_at) == 1:
            _i = _diff_at[0]
            _off = _first_diff_offset(anchor[_i], window[_i])
            return (f"differs in {differing} of {len(anchor)} lines -- "
                    f"first byte diverges at line {_i + 1}, offset {_off}")
    return f"differs in {differing} of {len(anchor)} lines"


def _first_diff_offset(a: str, b: str) -> int:


















    ab = a.encode("utf-8", errors="surrogatepass")
    bb = b.encode("utf-8", errors="surrogatepass")
    for _idx, (_ca, _cb) in enumerate(zip(ab, bb)):
        if _ca != _cb:
            return _idx
    return min(len(ab), len(bb))


def _edit_nearest_hint(old: str, lines: List[str], path: str = "") -> str:








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

    rivals = [i for _r, i in cands[1:] if abs(i - best_i) >= n]








    if rivals or tie_floor:
        total = tie_floor if tie_floor > 1 + len(rivals) else 1 + len(rivals)
        at_least = "at least " if total > 1 + len(rivals) else ""
        picked = ([best_i] + rivals)[:3]
        shown = ", ".join(str(i) for i in picked)



        extra = total - len(picked)
        more = f" and {extra} more" if extra > 0 else ""



        return (f"cannot suggest a nearest match: {at_least}{total} places "
                f"score the same ({_nearest_pct(best, False)}) — lines "
                f"{shown}{more}. The anchor does not tell them apart; "
                f"re-anchor on a longer or more distinctive block")





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




    longest = max([len(ln) for ln in anchor]
                  + [len(ln) for ln in lines[start - 1:start - 1 + n]])
    if n > 1:
        longest = max(longest, len("\n".join(anchor)))
    if longest <= _EDIT_NEAR_MAX_CHARS:
        return ""
    return f", scored on the first {_EDIT_NEAR_MAX_CHARS} characters"





_HEADER_ARG_MAX = 160
_HEADER_ANCHOR_MAX = 60

_SH_SUFFIXES = (".sh", ".bash", ".zsh", ".ksh")








_TRAILING_BACKSLASH_RUN = re.compile(r"(\\+)([ \t]*)\r?\n")





_WRITE_WARNINGS: List[Tuple[str, str]] = []












_PAYLOAD_PY_ESCAPE_ADVISORY: Dict[str, str] = {}







_MUTATION_ATTEMPTS: List[int] = [0]









_ARG_SEP: List[str] = [":"]






_CUSTOM_OP_OK: List[Optional[bool]] = [None]







_UNCLEAN_VALUE_EXITS: List[str] = []





_WRITE_COUNT: List[int] = [0]












_SKIP_COUNT: List[int] = [0]









_REAPPLY_COUNT: List[int] = [0]










_ROLLBACK_COUNT: List[int] = [0]


















_LEFT_ON_DISK_COUNT: List[int] = [0]


















_NOT_CHECKED: List[str] = []













_VALIDATED_FILES: List[Tuple[str, bool, bool]] = []


def _drop_write_warnings(path: str) -> None:

    key = os.path.abspath(path)
    _WRITE_WARNINGS[:] = [w for w in _WRITE_WARNINGS if w[0] != key]


def _retract_write(path: str) -> None:








    _drop_write_warnings(path)
    _bump_counter(_ROLLBACK_COUNT, "cnt_rollback")
    _bump_counter(_WRITE_COUNT, "cnt_write", by=-1)


def _rollback_action(pre_existed: bool, pre_content: Optional[bytes]) -> str:




















    if not pre_existed:
        return "unlink"
    if pre_content is not None:
        return "restore"
    return "refuse"


def _receipt_head(body: object) -> str:







    if isinstance(body, str):
        for ln in body.splitlines():
            if ln.strip():
                return _flat_cell(ln.strip())
    return ""


def _note_left_on_disk() -> None:






    _bump_counter(_LEFT_ON_DISK_COUNT, "cnt_left_on_disk")


def _left_on_disk_line(names: Sequence[str], path: str, body: object,
                       target: str = "") -> str:






















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
























    head = _receipt_head(body)
    quoted = f' — retracts "{head}"' if head else ""




    undo = "removed" if created else "restored"
    tail = "created" if created else "edited"




    subject = _flat_cell(path)
    via = ""
    if target and os.path.abspath(target) != os.path.abspath(path):
        subject = _flat_cell(target)
        via = (f" ({_flat_cell(path)} is a symlink, so the write landed on its "
               f"target and that is what was undone; the link is intact)")
    return (f"[rolled back] {name} {verb}; {subject} {undo}{via}{quoted}"
            f"; the file was NOT {tail}")


def _elide(s: str, limit: int) -> str:





    s = s.replace("\r\n", "⏎").replace("\n", "⏎")
    if len(s) <= limit:
        return s
    return f"{s[:limit]}… (+{len(s) - limit} chars)"


def _commit_header_arg(parts: List[str]) -> str:


















    msg = parts[1] if len(parts) > 1 else ""
    paths = [p for p in parts[2:] if p]



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























    return f"{op}:@payload" + (f" → {target}" if target else "")












_BATCH_POSITIONAL_FIELDS: Dict[str, Tuple[str, ...]] = {
    "head":        ("path", "n"),
    "tail":        ("path", "n"),
    "tree":        ("path", "depth"),
    "around_line": ("path", "line", "n"),
    "diff":        ("path1", "path2"),






    "batch":       ("path",),
}


def _ordered_batch_fields(op: str, item: Dict[str, Any]) -> Tuple[List[str], str]:















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






        pos = bisect.bisect_right(new_idxs, old_idx) - 1
        if pos >= 0 and new_idxs[pos] >= lo:
            count += 1
    return count




def _newline_census(text: str) -> Tuple[int, int, int]:





    crlf = text.count("\r\n")
    lf = text.count("\n") - crlf
    cr = text.count("\r") - crlf
    return crlf, lf, cr


def _newline_used(text: str) -> str:






    for nl in ("\r\n", "\r", "\n"):
        if nl in text:
            return nl
    return ""


def _newline_note(content: str, wrote: str = "", retermed: bool = False) -> str:


























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









    flat = text.replace("\r\n", "\n").replace("\r", "\n")
    out: List[Tuple[str, str]] = [("", text)]
    for nl in ("\r\n", "\r", "\n"):
        cand = flat.replace("\n", nl)
        if cand != text:
            out.append((nl, cand))
    return out


def _retermed(text: str, nl: str) -> str:

    return text.replace("\r\n", "\n").replace("\r", "\n").replace("\n", nl)


def _line_number_at(text: str, end: Optional[int] = None) -> int:











    return (text.count("\n", 0, end)
            + text.count("\r", 0, end)
            - text.count("\r\n", 0, end)) + 1


def _local_newline(lines: List[str], idx: int) -> str:














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





    if not old:
        return "ERROR: empty old string\n"
    if old == new:
        return "ERROR: old and new strings are identical\n"
    if not path:
        return "ERROR: empty path\n"
    if not os.path.isfile(path):




        return _path_not_found(path, label="file", op="edit", creates=True)

    try:








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











        crlf, lf, cr = _newline_census(content)
        used = [n for n, c in (("\r\n", crlf), ("\n", lf), ("\r", cr)) if c]
        if len(used) == 1 and _retermed(new, used[0]) != new:



            new, wrote, retermed = _retermed(new, used[0]), used[0], True
        elif len(used) > 1:



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


    start_line = _line_number_at(content, idx)



    new_lines = _split_lines(new_content)
    new_block_line_count = _line_number_at(new)
    end_line = start_line + new_block_line_count - 1
    ctx_start = max(1, start_line - 2)
    ctx_end = min(len(new_lines), end_line + 2)







    out = [f"edited {_flat_field(path, disclose_newline=True)} (line {start_line}"]
    if end_line != start_line:
        out.append(f"-{end_line}")
    out.append(")\n")
    _nl_note = _newline_note(new_content, wrote, retermed=retermed)
    if _nl_note:
        out.append(_nl_note)




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










_PASTE_BACKUP_MAX_BYTES = 8 * 1024 * 1024


def _paste_snapshot(path: str, new_content: str) -> Tuple[str, str]:































    try:
        with open(path, "rb") as fh:
            old = fh.read()
    except OSError as e:
        return "", f"could not read the outgoing bytes ({type(e).__name__})"
    if old == new_content.encode("utf-8", errors="surrogateescape"):

        return "", ""
    if len(old) > _PASTE_BACKUP_MAX_BYTES:
        return "", (
            f"{len(old)} bytes is over the {_PASTE_BACKUP_MAX_BYTES}-byte "
            f"copy limit"
        )














    try:
        snap_mode = stat.S_IMODE(os.stat(path).st_mode)
    except OSError:
        snap_mode = 0o600
    store = _cache_root() / _PASTE_BACKUP_KIND



    dest = store / "{}-{}.bak".format(
        hashlib.sha1(
            os.path.abspath(path).encode("utf-8", errors="surrogateescape")
        ).hexdigest()[:16],
        time.time_ns(),
    )











    if store.is_symlink():
        return "", f"{store} is a symlink, not a directory"






    use_dir_fd = hasattr(os, "O_DIRECTORY") and os.open in os.supports_dir_fd
    dir_fd = None
    try:
        store.mkdir(parents=True, exist_ok=True)
        if use_dir_fd:
            dir_fd = os.open(
                str(store), os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
            )






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


















    if os.name == "nt":
        return ""
    try:
        mode = stat.S_IMODE(os.stat(snapshot).st_mode)
    except OSError:
        return ""
    return " (mode {0:04o}, the file's own)".format(mode)


def _created_mode_note(path: str, content: str) -> str:





















    if os.name == "nt":
        return ""
    try:
        mode = stat.S_IMODE(os.stat(path).st_mode)
    except OSError:



        return ""
    note = "  ↳ mode {0:04o}, from the process umask\n".format(mode)
    if content.startswith("#!") and not (mode & 0o111):
        note += (
            "  ↳ starts with `#!` but is not executable — "
            "`chmod +x {0}` to run it\n".format(path)
        )
    return note


def op_paste(path: str, content: str) -> str:















    if not path:
        return "ERROR: empty path\n"



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

    if content and not content.endswith("\n"):
        content += "\n"
    existed = os.path.isfile(path)
    old_size = os.path.getsize(path) if existed else 0


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











    if not path:
        return "ERROR: empty path\n"
    if not content:
        return "ERROR: empty content — nothing to append\n"


    try:
        safe_resolved = _safe_path(path)
    except SecurityError as e:
        return f"ERROR: {e}\n"
    if os.path.isdir(path):




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






            with open(path, "r", encoding="utf-8", errors="surrogateescape",
                      newline="") as f:
                orig = f.read()
        except OSError as e:
            return f"ERROR: failed to read {path}: {e}\n"





    orig_lines = _split_lines_keepends(orig) if orig else []
    conv = _local_newline(orig_lines, len(orig_lines) - 1) if orig_lines else "\n"

    newline_hint = ""
    if orig and not orig.endswith(("\n", "\r")):



        orig += conv
        newline_hint = " [added the missing trailing newline first]"






    block = content if content.endswith(("\n", "\r")) else content + conv

    old_size = len(orig.encode("utf-8", errors="surrogateescape")) if existed else 0
    new_content = orig + block
    try:
        _atomic_write(path, new_content)
    except OSError as e:
        return f"ERROR: failed to write {path}: {e}\n"

    all_lines = _split_lines(new_content)





    added = len(_split_lines_keepends(block))
    start_line = len(all_lines) - added + 1
    new_size = len(new_content.encode("utf-8", errors="surrogateescape"))
    verb = "appended to" if existed else "created"
    out = [
        f"{verb} {path}: {added} lines at {start_line}-{len(all_lines)} "
        f"({old_size} → {new_size} bytes){newline_hint}\n"
    ]




    _nl_note = _newline_note(new_content, _newline_used(block))
    if _nl_note:
        out.append(_nl_note)
    if not existed:




        out.append(_created_mode_note(path, new_content))




    ctx_start = max(1, start_line - 2)
    shown_end = min(len(all_lines), start_line + _APPEND_RECEIPT_LINES - 1)
    for ln in range(ctx_start, shown_end + 1):
        marker = "→" if ln >= start_line else " "
        out.append(f"  {ln:>5} {marker} {all_lines[ln - 1]}\n")
    if shown_end < len(all_lines):
        out.append(f"  … (+{len(all_lines) - shown_end} more appended lines)\n")
    return "".join(out)


def op_replace_lines(path: str, start: int, end: int, content: str) -> str:









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





        with open(path, "r", encoding="utf-8", errors="surrogateescape",
                  newline="") as f:
            orig = f.read()
    except OSError as e:
        return f"ERROR: failed to read {path}: {e}\n"





    orig_lines = _split_lines_keepends(orig)
    total = len(orig_lines)

    if start > total + 1:
        return f"ERROR: start ({start}) > file length ({total}) + 1\n"

    insert_only = end < start


    clamped_hint = ""
    if not insert_only and end == total + 1:
        clamped_hint = f" [autocorrect: end ({end}) clamped to file length ({total})]"
        end = total
    if not insert_only and end > total:
        return f"ERROR: end ({end}) > file length ({total})\n"











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




    _nl_note = _newline_note(new_content,
                             _newline_used(new_block) if added else "")
    if _nl_note:
        out.append(_nl_note)

    ctx_start = max(1, new_start - 2)
    ctx_end = min(len(new_lines), max(new_end, new_start) + 2)
    for ln in range(ctx_start, ctx_end + 1):
        marker = "→" if added > 0 and new_start <= ln <= new_end else " "



        text = new_lines[ln - 1].rstrip("\r\n")
        out.append(f"  {ln:>5} {marker} {text}\n")
    return "".join(out)
