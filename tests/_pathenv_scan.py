"""Static scan for a hand-rolled PATH-stripping env at a Python spawn (#1151).

Why a static check at all: the defect cannot be reproduced on POSIX. None of
`_winenv._KEEP`'s names exist in a POSIX environ, so ``{"PATH": ""}`` and
``empty_path_env()`` are byte-identical here, and the only evidence a site is
wrong is a Windows CI log. Three occurrences (#658/#717, #725, #833, #1140)
were each found that way.

The specification is #1151's own post-mortem of the scanner that was cut:

  scope-aware  -- resolve a name only in the function that spawns. Module-wide
                  resolution false-positived `tests/test_git_env_scrub_692.py`.
  sys.executable only -- a bash or git child does not need SYSTEMROOT to start.
  three states -- ok / violation / unresolved / unreadable. Never fold "could
                  not tell" into "clean".

This module reads source. It imports nothing under test and runs no test code.
"""
from __future__ import annotations

import ast
from pathlib import Path
from typing import List, Optional

#: Names that make an env dict survivable for a Python child on Windows. The
#: guard's question is "could this child start", not "does this match the helper
#: exactly" -- a site keeping SYSTEMROOT by hand is not the defect.
#:
#: SYSTEMROOT and WINDIR *only*, and this is deliberately NARROWER than
#: `_winenv._KEEP`. `_winenv.py`'s docstring is explicit that these
#: two are what the interpreter needs to resolve its system DLLs and start at
#: all; TEMP/TMP/PATHEXT/COMSPEC are in that helper's `_KEEP` for behavioural
#: parity with the tool being tested, and none of them will start a process.
#: Accepting any of the four as evidence that a dict is survivable would call
#: `{"PATH": "", "TEMP": "x"}` clean -- the defect with a decoy in it.
_WINDOWS_ESSENTIALS = frozenset({"SYSTEMROOT", "WINDIR"})

_SPAWNS = frozenset({"run", "Popen", "call", "check_call", "check_output"})

_HELPERS = frozenset({"empty_path_env"})


class Finding:
    """One site, with the state the scanner could actually establish."""

    __slots__ = ("path", "lineno", "kind", "detail")

    def __init__(self, path: str, lineno: int, kind: str, detail: str) -> None:
        self.path = path
        self.lineno = lineno
        self.kind = kind
        self.detail = detail

    def describe(self) -> str:
        return "{0}:{1} [{2}] {3}".format(self.path, self.lineno, self.kind, self.detail)

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return "Finding(" + self.describe() + ")"


def _is_sys_executable(node: ast.AST) -> bool:
    return (
        isinstance(node, ast.Attribute)
        and node.attr == "executable"
        and isinstance(node.value, ast.Name)
        and node.value.id == "sys"
    )


def _spawns_the_running_interpreter(call: ast.Call, bindings=None) -> bool:
    """First positional arg is an argv list whose argv[0] is ``sys.executable``.

    Deliberately not "mentions sys.executable anywhere": a git spawn that
    happens to pass the interpreter path as an *argument* is still a git child,
    and it is argv[0] that decides what has to be able to start.

    ``bindings`` lets ``subprocess.run(argv, env=...)`` be recognised when
    ``argv`` is a local built one line up. Reading only a literal first argument
    made that whole call invisible -- not a violation, not even unresolved, just
    absent from the output, which is the failure mode this file exists to
    prevent rather than commit.
    """
    func = call.func
    name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", None)
    if name not in _SPAWNS:
        return False
    if not call.args:
        return False
    argv = call.args[0]
    if isinstance(argv, ast.Name) and bindings:
        argv = bindings.get(argv.id, argv)
    if not isinstance(argv, (ast.List, ast.Tuple)) or not argv.elts:
        return False
    return _is_sys_executable(argv.elts[0])


def _env_keyword(call: ast.Call) -> Optional[ast.AST]:
    for kw in call.keywords:
        if kw.arg == "env":
            return kw.value
    return None


def _dict_replaces_the_environment(node: ast.Dict) -> Optional[bool]:
    """True when this dict hands a Python child an env it cannot start from.

    ``None`` when the scanner cannot tell -- a non-literal key, or a ``**``
    spread, which is how ``{**os.environ, "PATH": ""}`` keeps SYSTEMROOT and is
    therefore not the defect.
    """
    names = set()
    for key in node.keys:
        if key is None:  # ``**something`` -- inherits whatever that carries
            return False
        if not (isinstance(key, ast.Constant) and isinstance(key.value, str)):
            return None
        names.add(key.value.upper())
    if "PATH" not in names:
        return False
    return not (names & _WINDOWS_ESSENTIALS)


def _calls_a_helper(node: ast.AST) -> bool:
    if not isinstance(node, ast.Call):
        return False
    func = node.func
    name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", None)
    return name in _HELPERS


def _reads_os_environ(node: ast.AST) -> bool:
    """Does this expression start from the real environment?

    ``dict(os.environ)``, ``os.environ.copy()``, ``dict(os.environ, X=...)``,
    ``{**os.environ, ...}``, a comprehension over ``os.environ.items()`` -- all
    of them carry SYSTEMROOT and WINDIR through, which is the entire property
    that matters here, so none of them can produce the defect.

    This is the scanner's deliberate limit, stated rather than hidden: a
    comprehension that read ``os.environ`` and kept only PATH *would* be the
    defect and would be called ok. That shape is `_winenv.empty_path_env()`
    written out by hand, minus its ``_KEEP`` set, and nobody has written it.
    The alternative -- calling all 33 environ-derived sites `unresolved` --
    makes the unresolved bucket the majority of the output, and a checker whose
    normal answer is "I could not tell" gets its assertion deleted within a
    month.
    """
    for sub in ast.walk(node):
        if (isinstance(sub, ast.Attribute) and sub.attr == "environ"
                and isinstance(sub.value, ast.Name) and sub.value.id == "os"):
            return True
    return False


def _local_bindings(scope: ast.AST) -> dict:
    """Names assigned in ``scope``'s own body -- never in a nested function.

    This is the whole of the scope-awareness, and the whole reason the previous
    scanner was cut: an ``env`` bound in a sibling function is not this
    function's ``env``, and reading it as such produced a confident false
    positive on a file nobody had touched. ``_nodes_directly_in`` rather than
    ``ast.walk`` is what makes that true rather than merely intended -- see its
    docstring.
    """
    bindings = {}
    for node in _nodes_directly_in(scope):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    bindings[target.id] = node.value
        elif isinstance(node, ast.AnnAssign):
            if isinstance(node.target, ast.Name) and node.value is not None:
                bindings[node.target.id] = node.value
    return bindings


def _classify(env: ast.AST, bindings: dict, returns=None, depth: int = 0) -> str:
    """``ok`` / ``violation`` / ``unresolved`` for one ``env=`` expression.

    ``returns`` maps a module-level function name to the expressions it returns,
    so ``env=_clean_env()`` is read rather than shrugged at. ``depth`` bounds
    that: a helper that returns a call to itself must not hang the suite.
    """
    if depth > 5:
        return "unresolved"
    if _calls_a_helper(env):
        return "ok"
    if _reads_os_environ(env):
        return "ok"
    if isinstance(env, ast.Dict):
        verdict = _dict_replaces_the_environment(env)
        if verdict is None:
            return "unresolved"
        return "violation" if verdict else "ok"
    if isinstance(env, ast.Name):
        bound = bindings.get(env.id)
        if bound is None:
            # Bound somewhere else, or a parameter, or never bound at all. The
            # honest answer is that this scanner did not look there.
            return "unresolved"
        return _classify(bound, {}, returns, depth + 1)
    if isinstance(env, ast.Call) and returns:
        func = env.func
        name = func.id if isinstance(func, ast.Name) else None
        exprs = returns.get(name) if name else None
        if exprs:
            verdicts = {
                _classify(e, _local_bindings_for_returns(name, returns), returns, depth + 1)
                for e in exprs
            }
            if "violation" in verdicts:
                return "violation"
            if verdicts == {"ok"}:
                return "ok"
        return "unresolved"
    return "unresolved"


def _local_bindings_for_returns(name, returns) -> dict:
    """Bindings visible to a resolved helper's ``return`` expression."""
    return (returns or {}).get("#bindings#" + str(name), {})


def _scopes(tree: ast.AST):
    """Every scope whose body can hold a spawn: the module and each function."""
    yield tree
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            yield node


def _nodes_directly_in(scope: ast.AST):
    """Every node in ``scope``'s body, stopping at each nested function scope.

    Explicitly NOT ``ast.walk``. ``ast.walk`` flattens the whole subtree into a
    queue before the consumer sees anything, so a ``continue`` on a nested
    ``FunctionDef`` inside a walk loop prunes nothing at all -- the nested
    body's nodes are yielded regardless, and both of this module's scope rules
    quietly stopped being scope rules. That is the exact cross-scope resolution
    #1151 says got the first scanner cut, reintroduced by a loop that looked
    like it was preventing it. Found in review of the first version of this
    file, where it was live in both directions: an inner binding resolved as an
    outer one, and an inner ``return`` attributed to the enclosing helper.
    """
    for stmt in getattr(scope, "body", []):
        stack = [stmt]
        while stack:
            node = stack.pop()
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
                continue
            yield node
            stack.extend(ast.iter_child_nodes(node))


def _calls_directly_in(scope: ast.AST):
    """Calls in ``scope``, excluding those inside a nested function scope."""
    for node in _nodes_directly_in(scope):
        if isinstance(node, ast.Call):
            yield node


def _module_level_returns(tree: ast.AST) -> dict:
    """``{function name: [returned expressions]}`` for module-level functions.

    A test that spawns with ``env=_clean_env()`` has not hidden anything -- the
    helper is right there in the same file. Reading it is the difference
    between a guard that answers and one whose commonest answer is a shrug.
    Keyed alongside each helper's own local bindings, so a helper that builds
    ``env`` in a local and returns it resolves too.
    """
    returns = {}
    for node in getattr(tree, "body", []):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        exprs = [
            sub.value for sub in _nodes_directly_in(node)
            if isinstance(sub, ast.Return) and sub.value is not None
        ]
        if exprs:
            returns[node.name] = exprs
            returns["#bindings#" + node.name] = _local_bindings(node)
    return returns


def _params_of(func: ast.AST) -> List[str]:
    a = func.args
    names = [p.arg for p in list(a.posonlyargs) + list(a.args) + list(a.kwonlyargs)]
    return names


def _env_forwarders(tree: ast.AST) -> dict:
    """``{helper name: parameter name}`` for helpers that spawn Python with it.

    The six sites this was written for all look like
    ``tests/test_validators_ruff.py``::

        def _spawn(*args, env=None):
            return subprocess.run([sys.executable, ADAPTER, *args], env=env)

    At the spawn, ``env`` is a parameter -- the scanner genuinely cannot tell
    what it holds, and that is the honest answer *there*. But it is the wrong
    place to ask: the decision is at every call site, which is exactly where
    the #1140 regression was written. So the helper is treated as a spawn and
    its callers are classified instead. Iterated to a fixpoint, because these
    files forward one more hop (``_run`` -> ``_spawn``).
    """
    funcs = [n for n in getattr(tree, "body", [])
             if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]
    forwarders = {}
    for func in funcs:
        params = _params_of(func)
        for call in _calls_directly_in(func):
            if not _spawns_the_running_interpreter(call):
                continue
            env = _env_keyword(call)
            if isinstance(env, ast.Name) and env.id in params:
                forwarders[func.name] = env.id
    changed = True
    while changed:
        changed = False
        for func in funcs:
            if func.name in forwarders:
                continue
            params = _params_of(func)
            for call in _calls_directly_in(func):
                callee = call.func.id if isinstance(call.func, ast.Name) else None
                if callee not in forwarders:
                    continue
                env = _env_keyword(call)
                if isinstance(env, ast.Name) and env.id in params:
                    forwarders[func.name] = env.id
                    changed = True
                    break
    return forwarders


def scan_source(source: str, path: str) -> List[Finding]:
    """Findings for one module's source. Never raises on bad input."""
    try:
        tree = ast.parse(source)
    except SyntaxError as e:
        return [Finding(path, getattr(e, "lineno", 0) or 0, "unreadable",
                        "could not parse this file, so it was not checked: " + str(e))]
    returns = _module_level_returns(tree)
    forwarders = _env_forwarders(tree)
    findings = []
    for scope in _scopes(tree):
        bindings = _local_bindings(scope)
        forwarded = forwarders.get(getattr(scope, "name", None))
        for call in _calls_directly_in(scope):
            callee = call.func.id if isinstance(call.func, ast.Name) else None
            spawns = _spawns_the_running_interpreter(call, bindings)
            if not spawns and callee not in forwarders:
                continue
            env = _env_keyword(call)
            if env is None:
                # No env= at all: the child inherits this process's environment,
                # which is the case that always worked.
                continue
            if (isinstance(env, ast.Name) and forwarded is not None
                    and env.id == forwarded):
                # This is the forwarding hop itself. Its callers carry the
                # decision and are classified in their own right; reporting it
                # here would report the same site once per helper in the chain.
                continue
            kind = _classify(env, bindings, returns)
            if kind == "ok":
                continue
            if kind == "violation":
                detail = ("a sys.executable spawn is handed a PATH-only env; use "
                          "_winenv.empty_path_env()")
            else:
                detail = "env= expression could not be evaluated by this scanner"
            findings.append(Finding(path, call.lineno, kind, detail))
    findings.sort(key=lambda f: (f.path, f.lineno))
    return findings


def scan_tree(root: Path) -> List[Finding]:
    """Findings for every ``test_*.py`` under ``root``."""
    findings = []
    for path in sorted(Path(root).glob("test_*.py")):
        findings.extend(
            scan_source(path.read_text(encoding="utf-8"), path.name))
    return findings
