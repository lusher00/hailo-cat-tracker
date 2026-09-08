#!/usr/bin/env python3

# Copyright (c) 2025 Ryan Lush <ryan.lush@gmail.com>
#
# Free for personal, educational, and open-source use.
# Commercial use requires written permission from the author.
# Contact: ryan.lush@gmail.com

"""
install_keybindings.py — put this repo's VS Code shortcuts and terminal
profiles where VS Code actually reads them.

WHY THIS EXISTS. VS Code has no workspace-level keybindings, and it ignores
terminal profiles that come from workspace settings (a cloned repo could
otherwise run arbitrary commands the moment you opened a terminal). So both of
the files in .vscode/ that matter here are inert templates:

    .vscode/keybindings.example.json  ->  ~/.../User/keybindings.json
    .vscode/settings.example.json     ->  ~/.../User/settings.json

Both have to be copied out by hand, and every reference in them has to match
something character for character:

    runTask bindings      -> a task label in .vscode/tasks.json
    newWithProfile bindings -> a profile name in settings.example.json

When one does not match, VS Code reports nothing at all. A bad task label
silently opens the task picker; a bad profile name silently does nothing. Both
look exactly like "my hotkey stopped working". Renaming a task or a profile
therefore breaks shortcuts with no error, which is what this script is here to
stop.

    ./tools/install_keybindings.py            # show what would change
    ./tools/install_keybindings.py --apply    # do it (backs up first)

Safe to re-run. It replaces only the entries it owns; anything else in your
keybindings.json and settings.json is left exactly as it was — including
comments, which are preserved by editing the settings file in place rather than
re-serialising it.
"""
import argparse, json, os, re, shutil, sys, datetime

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
EXAMPLE = os.path.join(REPO, ".vscode", "keybindings.example.json")
SETTINGS_EXAMPLE = os.path.join(REPO, ".vscode", "settings.example.json")
TASKS = os.path.join(REPO, ".vscode", "tasks.json")

USER_DIRS = [
    "~/Library/Application Support/Code/User",
    "~/Library/Application Support/Code - Insiders/User",
    "~/Library/Application Support/VSCodium/User",
    "~/Library/Application Support/Cursor/User",
    "~/.config/Code/User",
    "~/.config/Code - Insiders/User",
]

RUNTASK = "workbench.action.tasks.runTask"
NEWPROFILE = "workbench.action.terminal.newWithProfile"
PROFILE_KEY = ("terminal.integrated.profiles.osx" if sys.platform == "darwin"
               else "terminal.integrated.profiles.linux")


# ------------------------------------------------------------------ jsonc
def strip_jsonc(text):
    """Remove // and /* */ comments and trailing commas, respecting strings.

    VS Code writes these files with comments by default, so json.loads() fails
    on a perfectly normal one. Character-by-character rather than a regex
    because a URL inside a string ("https://...") must not look like a comment.
    """
    out, i, n = [], 0, len(text)
    in_str = in_line = in_block = False
    while i < n:
        c, nxt = text[i], text[i + 1] if i + 1 < n else ""
        if in_line:
            if c == "\n":
                in_line = False
                out.append(c)
        elif in_block:
            if c == "*" and nxt == "/":
                in_block = False
                i += 1
        elif in_str:
            out.append(c)
            if c == "\\":
                if i + 1 < n:
                    out.append(nxt)
                    i += 1
            elif c == '"':
                in_str = False
        else:
            if c == "/" and nxt == "/":
                in_line = True
                i += 1
            elif c == "/" and nxt == "*":
                in_block = True
                i += 1
            elif c == '"':
                in_str = True
                out.append(c)
            else:
                out.append(c)
        i += 1
    return re.sub(r",(\s*[}\]])", r"\1", "".join(out))


def load_jsonc(path, default):
    if not os.path.exists(path):
        return default
    raw = open(path, encoding="utf-8").read()
    if not raw.strip():
        return default
    return json.loads(strip_jsonc(raw))


# ------------------------------------------------- in-place settings edit
def _skip_ws_comments(raw, i):
    n = len(raw)
    while i < n:
        if raw[i] in " \t\r\n":
            i += 1
        elif raw.startswith("//", i):
            j = raw.find("\n", i)
            i = n if j < 0 else j + 1
        elif raw.startswith("/*", i):
            j = raw.find("*/", i)
            i = n if j < 0 else j + 2
        else:
            break
    return i


def top_level_key_span(raw, key):
    """Byte span of `"key": <value>` at the top level, or None.

    Returned span covers the key through the end of its value, so a caller can
    splice a replacement in without disturbing a single other byte of the file.
    """
    i, n, depth = 0, len(raw), 0
    in_str = in_line = in_block = False
    str_start = -1
    while i < n:
        c, nxt = raw[i], raw[i + 1] if i + 1 < n else ""
        if in_line:
            if c == "\n":
                in_line = False
        elif in_block:
            if c == "*" and nxt == "/":
                in_block = False
                i += 1
        elif in_str:
            if c == "\\":
                i += 1
            elif c == '"':
                in_str = False
                if depth == 1 and raw[str_start + 1:i] == key:
                    j = _skip_ws_comments(raw, i + 1)
                    if j < n and raw[j] == ":":
                        v = _skip_ws_comments(raw, j + 1)
                        return (str_start, _end_of_value(raw, v))
        else:
            if c == "/" and nxt == "/":
                in_line = True
                i += 1
            elif c == "/" and nxt == "*":
                in_block = True
                i += 1
            elif c == '"':
                in_str = True
                str_start = i
            elif c in "{[":
                depth += 1
            elif c in "}]":
                depth -= 1
        i += 1
    return None


def _end_of_value(raw, i):
    n = len(raw)
    if raw[i] in "{[":
        depth, in_str = 0, False
        while i < n:
            c, nxt = raw[i], raw[i + 1] if i + 1 < n else ""
            if in_str:
                if c == "\\":
                    i += 1
                elif c == '"':
                    in_str = False
            elif c == '"':
                in_str = True
            elif raw.startswith("//", i):
                j = raw.find("\n", i)
                i = n if j < 0 else j
                continue
            elif raw.startswith("/*", i):
                j = raw.find("*/", i)
                i = n if j < 0 else j + 1
            elif c in "{[":
                depth += 1
            elif c in "}]":
                depth -= 1
                if depth == 0:
                    return i + 1
            i += 1
        return n
    # scalar
    in_str = False
    while i < n:
        c = raw[i]
        if in_str:
            if c == "\\":
                i += 1
            elif c == '"':
                in_str = False
        elif c == '"':
            in_str = True
        elif c in ",}\n":
            return i
        i += 1
    return n


def upsert_settings(raw, key, value, indent=4):
    """Set `key` to `value` in a jsonc document, preserving everything else."""
    pretty = json.dumps(value, indent=indent, ensure_ascii=False)
    pretty = pretty.replace("\n", "\n" + " " * indent)   # nest one level
    entry = f'{json.dumps(key)}: {pretty}'

    span = top_level_key_span(raw, key)
    if span:
        return raw[:span[0]] + entry + raw[span[1]:]

    stripped = raw.strip()
    if not stripped:
        return "{\n" + " " * indent + entry + "\n}\n"

    open_brace = raw.index("{")
    rest = raw[open_brace + 1:]
    sep = "" if _skip_ws_comments(rest, 0) >= len(rest.rstrip().rstrip("}")) else ","
    return (raw[:open_brace + 1] + "\n" + " " * indent + entry + sep
            + raw[open_brace + 1:])


# ------------------------------------------------------------------ main

def example_profiles():
    """The profiles declared in settings.example.json, whatever platform key
    they were written under.

    The repo declares one platform's key; the machine running this may be a
    different one. Read whichever is there, write to this host's — the profile
    bodies are just ssh invocations and are not platform-specific.
    """
    for k, v in load_jsonc(SETTINGS_EXAMPLE, {}).items():
        if k.startswith("terminal.integrated.profiles."):
            return v
    return {}


def main():
    ap = argparse.ArgumentParser(description=__doc__,
            formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--apply", action="store_true", help="write the changes")
    ap.add_argument("--file", help="path to keybindings.json (default: autodetect)")
    ap.add_argument("--settings", help="path to settings.json (default: alongside it)")
    ap.add_argument("--no-profiles", action="store_true",
                    help="only install keybindings, leave settings.json alone")
    a = ap.parse_args()

    wanted = load_jsonc(EXAMPLE, [])
    if not wanted:
        print(f"no bindings found in {EXAMPLE}", file=sys.stderr)
        return 1

    profiles = example_profiles()
    do_profiles = bool(profiles) and not a.no_profiles

    # Cross-check every reference before touching anything. Installing a
    # binding that names a task or a profile which does not exist just
    # reintroduces the silent-failure bug at the source, which is the whole
    # thing this is meant to prevent.
    labels = {t["label"] for t in load_jsonc(TASKS, {"tasks": []})["tasks"]}
    bad = []
    for b in wanted:
        cmd, args = b.get("command"), b.get("args")
        if cmd == RUNTASK:
            if not isinstance(args, str) or args not in labels:
                bad.append((b.get("key"), args, "no such task in tasks.json"))
        elif cmd == NEWPROFILE:
            name = args.get("profileName") if isinstance(args, dict) else None
            if not name:
                bad.append((b.get("key"), args, "missing profileName"))
            elif name not in profiles:
                bad.append((b.get("key"), name,
                            f"no such profile in {os.path.basename(SETTINGS_EXAMPLE)}"))
    if bad:
        print("REFUSING: these bindings point at something that does not exist:")
        for key, what, why in bad:
            print(f"    {key:<20} {what!r}  — {why}")
        print("  Fix .vscode/tasks.json, settings.example.json, or the example"
              " file so they agree.")
        return 1

    n_task = sum(1 for b in wanted if b.get("command") == RUNTASK)
    n_prof = sum(1 for b in wanted if b.get("command") == NEWPROFILE)
    print(f"  {len(wanted)} binding(s): {n_task} task, {n_prof} terminal profile"
          f" — all references resolve")

    # ---------------- locate the user files ----------------
    target = a.file
    if not target:
        found = [d for d in (os.path.expanduser(c) for c in USER_DIRS)
                 if os.path.isdir(d)]
        if not found:
            print("Could not find a VS Code user directory. Pass --file explicitly.",
                  file=sys.stderr)
            return 1
        target = os.path.join(found[0], "keybindings.json")
        if len(found) > 1:
            print(f"  (found {len(found)} editors; using {found[0]})")
    settings_path = a.settings or os.path.join(os.path.dirname(target), "settings.json")

    print(f"  keybindings: {target}")
    if do_profiles:
        print(f"  settings:    {settings_path}")

    existing = load_jsonc(target, [])
    if not isinstance(existing, list):
        print("existing keybindings.json is not a JSON array — not touching it",
              file=sys.stderr)
        return 1

    # Ours = any binding pointing at one of OUR task labels or profile names,
    # plus any stale one still on a key we own. That second part is the bit that
    # matters: renaming "sync only  (<glyphs>S)" to "sync" left an orphan behind
    # that silently did nothing.
    ours_tasks = {b["args"] for b in wanted if b.get("command") == RUNTASK}
    ours_profiles = {b["args"]["profileName"] for b in wanted
                     if b.get("command") == NEWPROFILE}
    ours_keys = {b["key"] for b in wanted}

    def is_ours(b):
        cmd, args = b.get("command"), b.get("args")
        if cmd == NEWPROFILE:
            name = args.get("profileName") if isinstance(args, dict) else None
            return name in ours_profiles or b.get("key") in ours_keys
        if cmd != RUNTASK:
            return False
        if isinstance(args, str):
            if args in ours_tasks:
                return True
            # historical labels carried the shortcut inline in brackets
            base = re.sub(r"\s*\(.*\)\s*$", "", args).strip()
            if base in ours_tasks or args.split("  ")[0].strip() in ours_tasks:
                return True
        return b.get("key") in ours_keys

    kept = [b for b in existing if not is_ours(b)]
    removed = [b for b in existing if is_ours(b)]

    foreign = [b for b in removed
               if b.get("command") == RUNTASK
               and isinstance(b.get("args"), str)
               and b["args"] not in ours_tasks and b["args"] not in labels]

    print()
    for b in removed:
        print(f"  - remove  {b.get('key','?'):<20} {b.get('args')!r}")
    if foreign:
        print()
        print("  WARNING: those removals name tasks this repo does not define —")
        print("  they almost certainly belong to another project sharing the")
        print("  chord. VS Code keybindings are global, so installing here takes")
        print("  the chord away from it, and there it will silently open the")
        print("  task picker. Give one of the two projects different chords.")
    for b in wanted:
        what = (b["args"] if isinstance(b["args"], str)
                else b["args"].get("profileName"))
        print(f"  + add     {b['key']:<20} {what!r}")
    print(f"  = keeping {len(kept)} unrelated binding(s) untouched")

    # ---------------- profiles ----------------
    settings_raw = ""
    merged = {}
    if do_profiles:
        if os.path.exists(settings_path):
            settings_raw = open(settings_path, encoding="utf-8").read()
        current = (load_jsonc(settings_path, {}) or {}).get(PROFILE_KEY, {})
        theirs = {k: v for k, v in current.items() if k not in profiles}
        merged = dict(theirs)
        merged.update(profiles)
        print()
        for name in profiles:
            verb = "update" if name in current else "add   "
            print(f"  ~ {verb}  profile {name!r}")
        if theirs:
            print(f"  = keeping {len(theirs)} of your own profile(s) untouched")

    if not a.apply:
        print("\n  dry run — nothing written. Re-run with --apply")
        return 0

    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")

    if os.path.exists(target):
        shutil.copy2(target, f"{target}.bak-{stamp}")
        print(f"\n  backed up {target}.bak-{stamp}")
    os.makedirs(os.path.dirname(target) or ".", exist_ok=True)
    header = (f"// Managed in part by {os.path.basename(REPO)}/tools/"
              f"{os.path.basename(__file__)}\n"
              "// Entries below that run a task or open a terminal profile for\n"
              f"// {os.path.basename(REPO)} are regenerated by that script;\n"
              "// anything else in this file is left alone.\n")
    with open(target, "w", encoding="utf-8") as f:
        f.write(header + json.dumps(kept + wanted, indent=4, ensure_ascii=False) + "\n")
    json.loads(strip_jsonc(open(target, encoding="utf-8").read()))
    print(f"  wrote {target}  ({len(kept) + len(wanted)} bindings, valid JSON)")

    if do_profiles:
        if os.path.exists(settings_path):
            shutil.copy2(settings_path, f"{settings_path}.bak-{stamp}")
            print(f"  backed up {settings_path}.bak-{stamp}")
        new_raw = upsert_settings(settings_raw, PROFILE_KEY, merged)
        json.loads(strip_jsonc(new_raw))          # verify before writing
        os.makedirs(os.path.dirname(settings_path) or ".", exist_ok=True)
        with open(settings_path, "w", encoding="utf-8") as f:
            f.write(new_raw)
        print(f"  wrote {settings_path}  ({len(merged)} terminal profiles, valid JSON)")

    print("\n  VS Code picks both up immediately — no restart needed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
