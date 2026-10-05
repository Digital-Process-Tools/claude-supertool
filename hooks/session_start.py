
















from __future__ import annotations

import os
import subprocess
import sys


def _plugin_root() -> str:
    return os.environ.get("CLAUDE_PLUGIN_ROOT", "")


def _say(line: str) -> None:
    sys.stdout.write(line + "\n")
    sys.stdout.flush()


def _in_foreign_supertool_tree(bin_path: str) -> bool:


    d = os.path.realpath(os.getcwd())
    while d:
        if os.path.isfile(os.path.join(d, ".supertool.json")):
            local = os.path.join(d, "supertool.py")
            if not os.path.isfile(local):
                return False
            try:
                return not os.path.samefile(local, bin_path)
            except OSError:
                return True
        parent = os.path.dirname(d)
        if parent == d:
            break
        d = parent
    return False


def _own_stale_symlink_version(target: str, bin_path: str):


    if not os.path.exists(target):
        return None
    plugin_dir = os.path.dirname(os.path.dirname(bin_path))
    target_dir = os.path.dirname(os.path.dirname(target))
    if os.path.basename(target) != os.path.basename(bin_path):
        return None
    if not target_dir or target_dir != plugin_dir:
        return None
    return os.path.basename(os.path.dirname(target))


def _link(bin_path: str) -> bool:

    try:
        if os.path.lexists("supertool"):
            os.remove("supertool")
        os.symlink(bin_path, "supertool")
        return True
    except OSError:
        return False


def wrapper(bin_path: str) -> None:
    lexists = os.path.lexists("supertool")
    is_link = os.path.islink("supertool")
    if _in_foreign_supertool_tree(bin_path):
        _say("> No ./supertool wrapper created here: this directory is its own supertool tree, so a wrapper pointing at the plugin install would run the plugin core against this tree's config and presets — the mix every custom op declines (#678).")
        _say("> Use: python3 supertool.py 'op:args' — core, config and presets from one tree.")
        if lexists:
            _say("> Something is already at ./supertool and is left untouched. If it points at the plugin install, it is the broken wrapper described above.")
        return
    target = os.readlink("supertool") if is_link else None
    if is_link and target == bin_path:
        return
    old = _own_stale_symlink_version(target, bin_path) if is_link else None
    if old is not None:
        new = os.path.basename(os.path.dirname(bin_path))
        if _link(bin_path):
            _say(f"> ./supertool pointed at this plugin's own {old} — its own symlink from an earlier release, not a stranger's file. The plugin is now {new}; repointed it so calls are answered by the current version.")
        else:
            _say(f"> ./supertool pointed at this plugin's own {old} — its own symlink from an earlier release, not a stranger's file. The plugin is now {new}, but repointing it failed; calls will still be answered by {old} until this is fixed by hand.")
        return
    if lexists:
        _say("> ./supertool already exists here and is not the plugin symlink — leaving it untouched.")
        return
    _link(bin_path)


def onboard(bin_path: str) -> None:



    rc = subprocess.call([sys.executable, bin_path, "introduction", "output-format", "ops:session"])
    if rc != 0:
        _say("> supertool's op listing is incomplete: the interpreter ran and supertool exited non-zero. The ./supertool wrapper still works; 'ops' prints the listing.")


    if os.path.isfile(os.path.join(os.path.dirname(bin_path), "presets", "watch.json")):
        subprocess.call([sys.executable, bin_path, "channel:stranded"])


def main() -> int:



    bin_path = _plugin_root() + "/supertool.py"
    try:
        wrapper(bin_path)
        onboard(bin_path)
    except Exception as exc:  
        _say("> supertool's session hook raised " + type(exc).__name__ + ": "
             + str(exc) + ". The ./supertool wrapper may be missing; 'ops' prints the listing.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
